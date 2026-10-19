"""Server-owned sessions and ordered, durable conversation messages."""

from db.travel_models import metadata, chat_sessions, chat_messages

VERSION = "002_sessions"


def upgrade(connection):
    metadata.create_all(connection, tables=[chat_sessions, chat_messages])
