"""Full-network preparation and bounded airport-local flight search."""

import unittest
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine, insert, inspect

from db.travel_models import airports, demo_state, flight_offers, flights, metadata
from migrations import v005_network, v006_demo_state
from scripts.import_network_data import FIXTURE, prepare_snapshot
from services.actions import _offer
from services.demo_pricing import network_fare
from services.travel import find_flights


class NetworkTests(unittest.TestCase):
    def test_snapshot_uses_fictional_flights_and_stable_offers(self):
        as_of = date(2026, 10, 17)
        airport_rows, flight_rows, offer_rows, report = prepare_snapshot(FIXTURE, as_of)
        self.assertEqual((len(airport_rows), len(flight_rows), len(offer_rows)),
                         (115, 33121, 15374))
        self.assertEqual(report["scheduled_routes"], 618)
        by_id = {row["id"]: row for row in flight_rows}
        offers = {row["flight_id"]: row for row in offer_rows}
        self.assertEqual(by_id[8144]["flight_no"], "SIM-8144")
        self.assertEqual(by_id[8144]["departure_at"].astimezone(
            __import__("zoneinfo").ZoneInfo("Europe/Berlin")).date(),
            as_of + timedelta(days=1))
        self.assertEqual(offers[8144]["amount"], network_fare(8144, "HAM", "BSL"))
        self.assertEqual(offers[8152]["amount"], network_fare(8152, "HAM", "BSL"))
        self.assertNotIn(8144, [row["flight_id"] for row in offer_rows if
                          by_id[row["flight_id"]]["status"] != "Scheduled"])

    def test_search_filters_in_sql_and_caps_results(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        today = date(2026, 10, 17)
        with engine.begin() as db:
            metadata.create_all(db)
            db.execute(insert(airports), [
                dict(code="HAM", name="Hamburg", city="Hamburg", timezone="Europe/Berlin"),
                dict(code="BSL", name="Basel", city="Basel", timezone="Europe/Zurich"),
            ])
            db.execute(insert(flights), [dict(
                id=number, flight_no=f"SIM-{number}",
                departure_at=datetime(2026, 10, 18, 15, minute, tzinfo=timezone.utc),
                arrival_at=datetime(2026, 10, 18, 17, minute, tzinfo=timezone.utc),
                departure_airport="HAM", arrival_airport="BSL", status="Scheduled",
            ) for number, minute in ((1, 43), (2, 58))])
            db.execute(insert(flight_offers), [
                dict(flight_id=1, amount=7000, version=1, rule_id="flight-offer-demo-v1"),
                dict(flight_id=2, amount=5000, version=1, rule_id="network-fare-v1"),
            ])
            self.assertEqual(_offer(db, 2)["amount"], 5000)
            self.assertEqual([row["id"] for row in find_flights(
                db, "HAM", "BSL", today + timedelta(days=1),
                today=today, limit=1)], [1])
            self.assertEqual([row["id"] for row in find_flights(
                db, "HAM", "BSL", today + timedelta(days=1),
                today=today, sort="price", limit=1, offset=0)], [2])
            self.assertEqual([row["id"] for row in find_flights(
                db, "HAM", "BSL", today + timedelta(days=1),
                today=today, sort="price", limit=1, offset=1)], [1])
            self.assertEqual(find_flights(db, "HAM", "BSL", today, today=today), [])
            self.assertEqual(find_flights(db, "HAM", "BSL", today + timedelta(days=31),
                                          today=today), [])
            self.assertEqual(find_flights(db, "BSL", "HAM", today + timedelta(days=1),
                                          today=today), [])
        engine.dispose()

    def test_v005_adds_columns_to_old_schema_and_can_repeat(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as db:
            db.exec_driver_sql("CREATE TABLE airports (code varchar(8) PRIMARY KEY)")
            db.exec_driver_sql("CREATE TABLE flights (id integer PRIMARY KEY, "
                                  "departure_airport varchar(8), arrival_airport varchar(8), "
                                  "status varchar(30), departure_at timestamp)")
            v005_network.upgrade(db)
            v005_network.upgrade(db)
            self.assertTrue({"latitude", "longitude"}.issubset(
                {row["name"] for row in inspect(db).get_columns("airports")}))
            self.assertTrue({"actual_departure_at", "actual_arrival_at"}.issubset(
                {row["name"] for row in inspect(db).get_columns("flights")}))
            self.assertIn("ix_flights_route_status_departure",
                          {row["name"] for row in inspect(db).get_indexes("flights")})
        engine.dispose()

    def test_search_uses_imported_demo_date_after_host_date_changes(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        demo_day = date.today() + timedelta(days=15)
        departure = datetime.combine(demo_day + timedelta(days=1),
                                     datetime.min.time(), timezone.utc) + timedelta(hours=15)
        with engine.begin() as db:
            metadata.create_all(db)
            v006_demo_state.upgrade(db)
            db.execute(insert(demo_state).values(
                id=1, as_of=demo_day, fixture_sha256="a" * 64))
            db.execute(insert(airports), [
                dict(code="HAM", name="Hamburg", city="Hamburg", timezone="Europe/Berlin"),
                dict(code="BSL", name="Basel", city="Basel", timezone="Europe/Zurich"),
            ])
            db.execute(insert(flights).values(
                id=10, flight_no="SIM-10", departure_at=departure,
                arrival_at=departure + timedelta(hours=2),
                departure_airport="HAM", arrival_airport="BSL", status="Scheduled"))
            db.execute(insert(flight_offers).values(
                flight_id=10, amount=5000, version=1, rule_id="network-fare-v1"))
            self.assertEqual([row["id"] for row in find_flights(
                db, "HAM", "BSL", demo_day + timedelta(days=1))], [10])
            self.assertEqual(find_flights(db, "HAM", "BSL", demo_day), [])
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
