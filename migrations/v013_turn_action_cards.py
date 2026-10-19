"""Keep issued action cards with the idempotent conversation response."""

from sqlalchemy import inspect, text

VERSION = "013_turn_action_cards"


def upgrade(connection):
    columns = {column["name"] for column in inspect(connection).get_columns("chat_turns")}
    if "action_cards" not in columns:
        connection.execute(text("ALTER TABLE chat_turns ADD COLUMN action_cards JSON"))
