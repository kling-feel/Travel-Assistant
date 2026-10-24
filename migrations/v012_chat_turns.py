"""Add a durable per-request ledger for read-only graph turns."""

from travel_assistant.db.travel_models import chat_turns, metadata

VERSION = "012_chat_turns"


def upgrade(connection):
    metadata.create_all(connection, tables=[chat_turns])
