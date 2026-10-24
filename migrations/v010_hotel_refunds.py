"""Append the current hotel refund policy without changing historical rules."""

from sqlalchemy import insert, select

from travel_assistant.db.travel_models import policy_documents
from services.policy_catalog import HOTEL_POLICY

VERSION = "010_hotel_refunds"


def upgrade(connection):
    if connection.execute(select(policy_documents.c.id).where(
            policy_documents.c.id == HOTEL_POLICY["id"])).scalar_one_or_none() is None:
        connection.execute(insert(policy_documents).values(**HOTEL_POLICY))
