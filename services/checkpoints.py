"""Controlled PostgreSQL setup and request-scoped LangGraph persistence."""

import os

os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"

from langgraph.checkpoint.postgres import PostgresSaver  # noqa: E402

from db import engine


def _connection_string():
    if engine.dialect.name != "postgresql":
        raise RuntimeError("Persistent graph checkpoints require PostgreSQL")
    return engine.url.set(drivername="postgresql").render_as_string(hide_password=False)


def open_checkpointer():
    return PostgresSaver.from_conn_string(_connection_string())


def setup_checkpoints():
    with open_checkpointer() as saver:
        saver.setup()
