"""Print a read-only, credential-free summary of a known demo database."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from sqlalchemy import func, select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from travel_assistant.db import engine
from travel_assistant.db.travel_models import (
    action_results, airports, audit_events, chat_messages, chat_sessions, chat_turns,
    demo_state, flight_offers, flight_orders, flights, hotel_orders, hotel_products,
    hotels, pending_actions, policy_documents, rental_orders, rental_products, travelers,
)


def summarize(connection):
    connection.execute(text("SET TRANSACTION READ ONLY"))
    state = connection.execute(select(demo_state.c.as_of, demo_state.c.fixture_sha256)).first()
    tables = (travelers, airports, flights, flight_offers, hotels,
              hotel_products, rental_products, policy_documents,
              flight_orders, hotel_orders, rental_orders, chat_sessions,
              chat_messages, chat_turns, pending_actions, action_results,
              audit_events)
    counts = {table.name: connection.execute(select(func.count()).select_from(table)).scalar_one()
              for table in tables}
    order_statuses = {}
    digest = hashlib.sha256()
    for table in (flight_orders, hotel_orders, rental_orders):
        statuses = {}
        for row in connection.execute(select(table).order_by(table.c.id)).mappings():
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
            fields = {key: value for key, value in row.items()
                      if key not in ("created_at", "updated_at")}
            digest.update(json.dumps((table.name, fields), sort_keys=True,
                                     ensure_ascii=False, default=str).encode())
        order_statuses[table.name] = statuses
    versions = sorted(connection.execute(text("SELECT version FROM schema_migrations")).scalars())
    checkpoints = {}
    if connection.execute(text("SELECT to_regclass('public.checkpoints')")).scalar_one():
        for name in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            checkpoints[name] = connection.execute(text(f"SELECT count(*) FROM {name}")).scalar_one()
    supply = hashlib.sha256()
    for table in (airports, flights, flight_offers, hotels, hotel_products,
                  rental_products, policy_documents):
        for row in connection.execute(select(table).order_by(*table.primary_key.columns)).mappings():
            supply.update(json.dumps((table.name, dict(row)), sort_keys=True,
                                     ensure_ascii=False, default=str).encode())
    return {"as_of": state.as_of.isoformat() if state else None,
            "fixture_sha256": state.fixture_sha256 if state else None,
            "counts": counts, "order_statuses": order_statuses,
            "migration_versions": versions, "checkpoint_counts": checkpoints,
            "order_digest": digest.hexdigest(), "supply_digest": supply.hexdigest(),
            "non_demo_accounts": connection.execute(select(func.count()).select_from(travelers)
                .where(~travelers.c.username.in_(("demo1", "demo2")))).scalar_one()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-port", type=int, required=True)
    args = parser.parse_args()
    url = engine.url
    if (os.getenv("DATABASE_URL") or url.drivername != "postgresql+psycopg" or
            url.host != "127.0.0.1" or url.port != args.expected_port or
            url.database != "travel_demo" or url.username != "travel_app"):
        parser.error("Effective database target differs from the expected local port")
    with engine.connect() as connection:
        report = summarize(connection)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
