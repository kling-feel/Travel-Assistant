"""Add confirmed rental orders and two traveler-confirmed action types."""

from sqlalchemy import insert, select, text

from travel_assistant.db.travel_models import metadata, policy_documents, rental_orders
from services.policy_catalog import RENTAL_POLICY

VERSION = "009_rental_orders"


def upgrade(connection):
    metadata.create_all(connection, tables=[rental_orders])
    if connection.dialect.name == "postgresql":
        connection.execute(text(
            "ALTER TABLE pending_actions DROP CONSTRAINT pending_actions_action_type_check"
        ))
        connection.execute(text(
            "ALTER TABLE pending_actions ADD CONSTRAINT pending_actions_action_type_check "
            "CHECK (action_type IN ('flight_book', 'flight_change', 'flight_cancel', "
            "'hotel_book', 'hotel_change', 'hotel_cancel', 'rental_book', 'rental_cancel'))"
        ))
    if connection.execute(select(policy_documents.c.id).where(
            policy_documents.c.id == RENTAL_POLICY["id"])).scalar_one_or_none() is None:
        connection.execute(insert(policy_documents).values(**RENTAL_POLICY))
