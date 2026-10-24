"""Deterministic fictional offers; source tickets contain no candidate fares."""

from hashlib import sha256
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import insert, select

from travel_assistant.db.travel_models import flight_offers, flights
# v003 imports this historical name; current hotel transactions use policy_catalog.HOTEL_POLICY.
from travel_assistant.services.policy_catalog import FLIGHT_POLICY, HOTEL_POLICY_V3 as HOTEL_POLICY


FLIGHT_FARES = {"LH0130": 6400, "QR0055": 6800,
                "AF0137": 6700, "TK0081": 7000}
OFFER_RULE_ID = "flight-offer-demo-v1"
OFFER_VERSION = 1
NETWORK_FARE_ALGORITHM = "network-fare-v1"
NETWORK_OFFER_RULE_ID = NETWORK_FARE_ALGORITHM
def cancellation_fee(amount):
    return int((Decimal(amount) * Decimal("0.30")).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))


def network_fare(flight_id: int, departure: str, arrival: str) -> int:
    """Stable fictional amount; no airline price or model output is used."""
    key = f"{NETWORK_FARE_ALGORITHM}|{flight_id}|{departure}|{arrival}"
    value = int.from_bytes(sha256(key.encode("ascii")).digest()[:4], "big")
    return 4000 + value % 4001


def seed_missing_flight_offers(connection):
    """Backfill existing flights without overwriting a previously quoted offer."""
    existing = set(connection.execute(select(flight_offers.c.flight_id)).scalars())
    rows = connection.execute(select(flights.c.id, flights.c.flight_no)).all()
    for flight_id, flight_no in rows:
        amount = FLIGHT_FARES.get(flight_no)
        if flight_id not in existing and amount is not None:
            connection.execute(insert(flight_offers).values(
                flight_id=flight_id, amount=amount, version=OFFER_VERSION,
                rule_id=OFFER_RULE_ID,
            ))
