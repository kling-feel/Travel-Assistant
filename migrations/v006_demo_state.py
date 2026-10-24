"""Persist the demonstration date used for the current network snapshot."""

from travel_assistant.db.travel_models import demo_state, metadata

VERSION = "006_demo_state"


def upgrade(connection):
    metadata.create_all(connection, tables=[demo_state])
