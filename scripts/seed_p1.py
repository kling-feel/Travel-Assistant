"""Map the current P0 demo_source snapshot into formal P1 tables.

This resets only the two fictional demo accounts and their P1 orders. Run after
import_demo_data.py when starting a new demo day, not during an active demo.
"""

from pathlib import Path
from datetime import date, datetime, timedelta
import os
import sys

from sqlalchemy import delete, func, insert, inspect, select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import engine
from db.travel_models import (airports, flight_orders, flights, hotel_orders,
                              hotel_products, hotels, policy_documents, travelers,
                              chat_sessions, chat_messages, pending_actions,
                              flight_offers, action_results, audit_events, demo_state,
                              rental_orders, rental_products)
from services.demo_pricing import seed_missing_flight_offers
from services.policy_catalog import SEED_POLICIES
from utils.password_hash import get_hashed_password


def seed(connection, passwords):
    existing = connection.execute(select(travelers.c.username)).scalars().all()
    if any(name not in ("demo1", "demo2") for name in existing):
        raise RuntimeError("Formal tables contain non-demo accounts; refusing reset")
    source = {}
    for name in ("airports", "flights", "hotels", "travelers", "flight_orders"):
        source[name] = connection.execute(text(f"SELECT * FROM demo_source.{name}")).mappings().all()
    if (len(source["travelers"]), len(source["flight_orders"])) != (2, 2):
        raise RuntimeError("Expected exactly two demo travelers and flight orders")
    if inspect(connection).has_table(pending_actions.name):
        if connection.execute(select(func.count()).select_from(pending_actions).where(
                pending_actions.c.status == "pending")).scalar_one():
            raise RuntimeError("Pending actions exist; refusing demo reset")

    # A deliberate demo reset discards old conversations before rebuilding users.
    if inspect(connection).has_table(chat_sessions.name):
        if inspect(connection).has_table(pending_actions.name):
            if inspect(connection).has_table(action_results.name):
                connection.execute(delete(action_results))
            if inspect(connection).has_table(audit_events.name):
                connection.execute(delete(audit_events))
            connection.execute(delete(pending_actions))
        connection.execute(delete(chat_messages))
        connection.execute(delete(chat_sessions))
    if inspect(connection).has_table(flight_offers.name):
        connection.execute(delete(flight_offers))
    if inspect(connection).has_table(demo_state.name):
        connection.execute(delete(demo_state))
    if inspect(connection).has_table(rental_products.name):
        if inspect(connection).has_table(rental_orders.name):
            connection.execute(delete(rental_orders))
        connection.execute(delete(rental_products))
    for table in (hotel_orders, flight_orders, hotel_products, policy_documents,
                  hotels, flights, airports, travelers):
        connection.execute(delete(table))

    for row in source["travelers"]:
        number = int(row["demo_traveler_id"].rsplit("_", 1)[-1])
        connection.execute(insert(travelers).values(
            id=row["demo_traveler_id"], username=f"demo{number}",
            password_hash=get_hashed_password(passwords[number]),
            display_name=row["display_name"], active=1,
        ))
    for row in source["airports"]:
        connection.execute(insert(airports).values(
            code=row["airport_code"], name=row["airport_name"],
            city=row["city"], timezone=row["timezone"],
        ))
    for row in source["flights"]:
        connection.execute(insert(flights).values(
            id=row["source_flight_id"], flight_no=row["flight_no"],
            departure_at=(datetime.fromisoformat(row["scheduled_departure"])
                          if isinstance(row["scheduled_departure"], str)
                          else row["scheduled_departure"]),
            arrival_at=(datetime.fromisoformat(row["scheduled_arrival"])
                        if isinstance(row["scheduled_arrival"], str)
                        else row["scheduled_arrival"]),
            departure_airport=row["departure_airport"],
            arrival_airport=row["arrival_airport"], status=row["status"],
        ))
    if inspect(connection).has_table(flight_offers.name):
        seed_missing_flight_offers(connection)
    prices = {"Upper Midscale": 120, "Upper Upscale": 170, "Luxury": 220}
    for row in source["hotels"]:
        available_from = (date.fromisoformat(row["available_from"])
                          if isinstance(row["available_from"], str)
                          else row["available_from"])
        available_until = (date.fromisoformat(row["available_until"])
                           if isinstance(row["available_until"], str)
                           else row["available_until"])
        connection.execute(insert(hotels).values(
            id=row["source_hotel_id"], name=row["name"], city=row["city"],
            price_tier=row["price_tier"],
        ))
        connection.execute(insert(hotel_products).values(
            id=f"room-{row['source_hotel_id']}", hotel_id=row["source_hotel_id"],
            room_type="演示标准间", nightly_price=prices.get(row["price_tier"], 120),
            available_from=available_from, available_until=available_until,
        ))
    for row in source["flight_orders"]:
        connection.execute(insert(flight_orders).values(
            id=row["source_ticket_no"], traveler_id=row["demo_traveler_id"],
            flight_id=row["source_flight_id"], fare_conditions=row["fare_conditions"],
            amount=row["amount"] or 0, status=row["status"], version=1,
        ))
    first_hotel = source["hotels"][0]
    second_hotel = source["hotels"][1]
    for number, hotel, status in ((1, first_hotel, "confirmed"), (2, second_hotel, "cancelled")):
        product_id = f"room-{hotel['source_hotel_id']}"
        product = connection.execute(select(hotel_products).where(
            hotel_products.c.id == product_id
        )).mappings().one()
        connection.execute(insert(hotel_orders).values(
            id=f"demo-hotel-order-{number}", traveler_id=f"demo_traveler_{number}",
            product_id=product_id, check_in=product["available_from"],
            check_out=product["available_from"] + timedelta(days=1),
            total_price=product["nightly_price"], status=status, version=1,
        ))
    connection.execute(insert(policy_documents), list(SEED_POLICIES))


def main():
    passwords = {number: os.getenv(f"DEMO_TRAVELER_{number}_PASSWORD", "")
                 for number in (1, 2)}
    if any(len(value) < 12 or value.startswith("replace-with") for value in passwords.values()):
        raise RuntimeError("Run scripts/init_local_env.py to set two local demo passwords")
    with engine.begin() as connection:
        seed(connection, passwords)
    print(f"Seeded 2 demo accounts, 2 flight orders, 2 hotel orders, 3 hotel products, {len(SEED_POLICIES)} policies")


if __name__ == "__main__":
    main()
