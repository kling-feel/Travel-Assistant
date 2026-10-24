"""Add simulated nearby products without changing existing orders."""

from sqlalchemy import insert, inspect, select, text

from travel_assistant.db.travel_models import metadata, policy_documents, rental_products
from services.policy_catalog import ALL_POLICIES

VERSION = "007_city_products"


def upgrade(connection):
    hotel_columns = {column["name"] for column in inspect(connection).get_columns("hotels")}
    for name, definition in (
        ("nearby_airport", "VARCHAR(8) REFERENCES airports(code)"),
        ("latitude", "FLOAT"),
        ("longitude", "FLOAT"),
    ):
        if name not in hotel_columns:
            connection.execute(text(f"ALTER TABLE hotels ADD COLUMN {name} {definition}"))
    product_columns = {column["name"] for column in inspect(connection).get_columns("hotel_products")}
    if "generation_rule" not in product_columns:
        connection.execute(text("ALTER TABLE hotel_products ADD COLUMN generation_rule VARCHAR(80)"))
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_hotels_city_airport ON hotels (city, nearby_airport)"
    ))
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_hotel_products_dates "
        "ON hotel_products (available_from, available_until)"
    ))
    metadata.create_all(connection, tables=[rental_products])
    existing = set(connection.execute(select(policy_documents.c.id)).scalars())
    missing = [policy for policy in ALL_POLICIES if policy["id"] not in existing]
    if missing:
        connection.execute(insert(policy_documents), missing)
