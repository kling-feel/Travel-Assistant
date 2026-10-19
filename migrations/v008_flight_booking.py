"""Allow confirmed flight bookings and prevent duplicate active tickets."""

from sqlalchemy import text

VERSION = "008_flight_booking"


def upgrade(connection):
    if connection.dialect.name == "postgresql":
        duplicates = connection.execute(text(
            "SELECT traveler_id, flight_id FROM flight_orders "
            "WHERE status = 'confirmed' GROUP BY traveler_id, flight_id "
            "HAVING count(*) > 1 LIMIT 1"
        )).first()
        if duplicates:
            raise RuntimeError("Duplicate confirmed flight orders block v008")
        connection.execute(text(
            "ALTER TABLE pending_actions "
            "DROP CONSTRAINT pending_actions_action_type_check"
        ))
        connection.execute(text(
            "ALTER TABLE pending_actions ADD CONSTRAINT pending_actions_action_type_check "
            "CHECK (action_type IN ('flight_book', 'flight_change', 'flight_cancel', "
            "'hotel_book', 'hotel_change', 'hotel_cancel'))"
        ))
    connection.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_flight_orders_active_traveler_flight "
        "ON flight_orders (traveler_id, flight_id) WHERE status = 'confirmed'"
    ))
