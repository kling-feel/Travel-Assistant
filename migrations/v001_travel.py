"""Initial formal travel schema. P0 demo_source stays separate."""

from travel_assistant.db.travel_models import (metadata, travelers, airports, flights, hotels,
                              hotel_products, flight_orders, hotel_orders,
                              policy_documents)

VERSION = "001_travel"


def upgrade(connection):
    metadata.create_all(connection, tables=[
        travelers, airports, flights, hotels, hotel_products,
        flight_orders, hotel_orders, policy_documents,
    ])
