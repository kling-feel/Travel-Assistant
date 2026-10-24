"""Apply all versioned travel schema changes to the configured PostgreSQL database."""

from pathlib import Path
import argparse
import os
import sys

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from travel_assistant.db import engine
from migrations import (v001_travel, v002_sessions, v003_actions,
                        v004_confirmations, v005_network, v006_demo_state,
                        v007_city_products, v008_flight_booking,
                        v009_rental_orders, v010_hotel_refunds,
                        v011_rental_changes, v012_chat_turns,
                        v013_turn_action_cards)

MIGRATIONS = (v001_travel, v002_sessions, v003_actions,
              v004_confirmations, v005_network, v006_demo_state,
              v007_city_products, v008_flight_booking,
              v009_rental_orders, v010_hotel_refunds,
              v011_rental_changes, v012_chat_turns,
              v013_turn_action_cards)


def assert_empty_batch_database(connection, expected_port: int):
    """Check the effective target before the first schema write."""
    if expected_port == 5433:
        raise RuntimeError("The existing demo database port cannot be a fresh batch target")
    url = engine.url
    if (os.getenv("DATABASE_URL") or url.drivername != "postgresql+psycopg" or
            url.host != "127.0.0.1" or url.port != expected_port or
            url.database != "travel_demo" or url.username != "travel_app"):
        raise RuntimeError("Effective database target does not match the new batch")
    if connection.dialect.name != "postgresql":
        raise RuntimeError("New batch requires PostgreSQL")
    tables = connection.execute(text(
        "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
    )).scalar_one()
    if tables:
        raise RuntimeError("New batch database already has tables")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh-batch-port", type=int,
                        help="Require a specific isolated port and an empty database")
    args = parser.parse_args()
    if args.fresh_batch_port is not None and not 1 <= args.fresh_batch_port <= 65535:
        parser.error("--fresh-batch-port must be a valid port")
    with engine.begin() as connection:
        if args.fresh_batch_port is not None:
            assert_empty_batch_database(connection, args.fresh_batch_port)
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version varchar(80) PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        ))
        for migration in MIGRATIONS:
            applied = connection.execute(
                text("SELECT 1 FROM schema_migrations WHERE version = :version"),
                {"version": migration.VERSION},
            ).scalar_one_or_none()
            if not applied:
                migration.upgrade(connection)
                connection.execute(
                    text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                    {"version": migration.VERSION},
                )
                print("Applied", migration.VERSION)
            else:
                print("Already applied", migration.VERSION)
    from travel_assistant.services.checkpoints import setup_checkpoints
    setup_checkpoints()


if __name__ == "__main__":
    main()
