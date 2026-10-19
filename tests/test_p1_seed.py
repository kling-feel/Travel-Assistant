"""Seed mapping is repeatable and keeps the two demo accounts separate."""

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.pool import StaticPool

from db.travel_models import (action_results, audit_events, chat_messages, chat_sessions, flight_offers,
                              flight_orders, flights, hotel_orders, hotel_products,
                              metadata, pending_actions, travelers)
from scripts.seed_p1 import seed


class SeedTests(unittest.TestCase):
    def test_seed_twice_replaces_only_demo_snapshot(self):
        engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
        with engine.begin() as connection:
            connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS demo_source")
            for ddl in (
                "CREATE TABLE demo_source.airports (airport_code text, airport_name text, city text, timezone text)",
                "CREATE TABLE demo_source.flights (source_flight_id integer, flight_no text, scheduled_departure text, scheduled_arrival text, departure_airport text, arrival_airport text, status text)",
                "CREATE TABLE demo_source.hotels (source_hotel_id integer, name text, city text, price_tier text, available_from text, available_until text)",
                "CREATE TABLE demo_source.travelers (demo_traveler_id text, display_name text, source_passenger_id text)",
                "CREATE TABLE demo_source.flight_orders (source_ticket_no text, demo_traveler_id text, source_flight_id integer, source_book_ref text, fare_conditions text, amount integer, status text)",
            ):
                connection.exec_driver_sql(ddl)
            connection.exec_driver_sql(
                "INSERT INTO demo_source.airports VALUES "
                "('HAM','Hamburg','Hamburg','Europe/Berlin'),"
                "('BSL','Basel','Basel','Europe/Zurich')")
            connection.exec_driver_sql(
                "INSERT INTO demo_source.flights VALUES "
                "(1,'LH0130','2026-10-18 14:00:00+00:00','2026-10-18 16:00:00+00:00','HAM','BSL','Scheduled'),"
                "(2,'QR0055','2026-10-18 15:00:00+00:00','2026-10-18 17:00:00+00:00','HAM','BSL','Scheduled')")
            connection.exec_driver_sql(
                "INSERT INTO demo_source.hotels VALUES "
                "(1,'Hotel One','Basel','Luxury','2026-10-18','2026-11-18'),"
                "(2,'Hotel Two','Basel','Upper Upscale','2026-10-18','2026-11-18')")
            connection.exec_driver_sql(
                "INSERT INTO demo_source.travelers VALUES "
                "('demo_traveler_1','One','anon1'),('demo_traveler_2','Two','anon2')")
            connection.exec_driver_sql(
                "INSERT INTO demo_source.flight_orders VALUES "
                "('ticket1','demo_traveler_1',1,'ref1','Economy',100,'confirmed'),"
                "('ticket2','demo_traveler_2',2,'ref2','Economy',120,'confirmed')")
            metadata.create_all(connection)
            seed(connection, {1: "local-password-one", 2: "local-password-two"})
            connection.execute(insert(chat_sessions).values(
                id="old-chat", traveler_id="demo_traveler_1", status="active"))
            connection.execute(insert(chat_messages).values(
                session_id="old-chat", sequence=1, role="user", content="old"))
            now = datetime.now(timezone.utc)
            connection.execute(insert(pending_actions).values(
                id="old-action", traveler_id="demo_traveler_1",
                session_id="old-chat", action_type="hotel_cancel",
                target_order_id="demo-hotel-order-1", target_order_version=1,
                parameters={}, before_snapshot={}, after_snapshot={},
                price_summary={}, policy_id="hotel-actions-demo-v2",
                policy_version=2, status="pending", idempotency_key="old-action",
                created_at=now, expires_at=now + timedelta(minutes=15)))
            connection.execute(insert(audit_events).values(
                action_id="old-action", traveler_id="demo_traveler_1",
                session_id="old-chat", event_type="proposed", summary={}, created_at=now))
            connection.execute(insert(action_results).values(
                action_id="old-action", traveler_id="demo_traveler_1",
                session_id="old-chat", order_id="demo-hotel-order-1",
                order_snapshot={}, price_summary={}, created_at=now))
            with self.assertRaisesRegex(RuntimeError, "Pending actions"):
                seed(connection, {1: "local-password-one", 2: "local-password-two"})
            self.assertEqual(connection.execute(select(pending_actions.c.status)).scalar_one(),
                             "pending")
            connection.execute(update(pending_actions).values(status="rejected"))
            seed(connection, {1: "local-password-one", 2: "local-password-two"})
            self.assertEqual(len(connection.execute(select(travelers)).all()), 2)
            self.assertEqual(len(connection.execute(select(flights)).all()), 2)
            self.assertEqual(len(connection.execute(select(hotel_products)).all()), 2)
            self.assertEqual(len(connection.execute(select(hotel_orders)).all()), 2)
            self.assertEqual(len(connection.execute(select(chat_sessions)).all()), 0)
            self.assertEqual(len(connection.execute(select(chat_messages)).all()), 0)
            self.assertEqual(len(connection.execute(select(pending_actions)).all()), 0)
            self.assertEqual(len(connection.execute(select(action_results)).all()), 0)
            self.assertEqual(len(connection.execute(select(audit_events)).all()), 0)
            self.assertEqual(sorted(connection.execute(select(flight_offers.c.amount)).scalars()),
                             [6400, 6800])
            self.assertEqual({row.traveler_id for row in connection.execute(
                select(flight_orders.c.traveler_id))},
                {"demo_traveler_1", "demo_traveler_2"})
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
