"""Durable proposals and versioned fictional candidate fares."""

from sqlalchemy import insert, select

from travel_assistant.db.travel_models import (flight_offers, metadata, pending_actions,
                              policy_documents)
from travel_assistant.services.demo_pricing import (FLIGHT_POLICY, HOTEL_POLICY,
                                   seed_missing_flight_offers)

VERSION = "003_actions"


def upgrade(connection):
    metadata.create_all(connection, tables=[flight_offers, pending_actions])
    for policy in (FLIGHT_POLICY, HOTEL_POLICY):
        exists = connection.execute(select(policy_documents.c.id).where(
            policy_documents.c.id == policy["id"])).scalar_one_or_none()
        if exists is None:
            connection.execute(insert(policy_documents).values(**policy))
    seed_missing_flight_offers(connection)
