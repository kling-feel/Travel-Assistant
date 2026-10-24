"""Apply all versioned travel schema changes to the configured PostgreSQL database."""

from pathlib import Path
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


def main():
    with engine.begin() as connection:
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
    from services.checkpoints import setup_checkpoints
    setup_checkpoints()


if __name__ == "__main__":
    main()
