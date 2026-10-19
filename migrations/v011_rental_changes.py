"""Allow rental change proposals and append the current fictional rental rule."""

from sqlalchemy import insert, select, text

from db.travel_models import policy_documents
from services.policy_catalog import CURRENT_RENTAL_POLICY

VERSION = "011_rental_changes"


def upgrade(connection):
    if connection.dialect.name == "postgresql":
        connection.execute(text(
            "ALTER TABLE pending_actions DROP CONSTRAINT pending_actions_action_type_check"
        ))
        connection.execute(text(
            "ALTER TABLE pending_actions ADD CONSTRAINT pending_actions_action_type_check "
            "CHECK (action_type IN ('flight_book', 'flight_change', 'flight_cancel', "
            "'hotel_book', 'hotel_change', 'hotel_cancel', 'rental_book', "
            "'rental_cancel', 'rental_change'))"
        ))
    if connection.execute(select(policy_documents.c.id).where(
            policy_documents.c.id == CURRENT_RENTAL_POLICY["id"])).scalar_one_or_none() is None:
        connection.execute(insert(policy_documents).values(**CURRENT_RENTAL_POLICY))
