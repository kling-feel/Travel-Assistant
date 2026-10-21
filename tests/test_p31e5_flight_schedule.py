"""Protect arrival-day decisions against the source fixture's short durations."""

import sqlite3
import unittest
from datetime import date, datetime, timedelta, timezone
from math import nan
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, insert

from db.travel_models import (airports as airport_table, demo_state,
                              hotel_products, hotels, metadata)
from scripts.import_network_data import FIXTURE, prepare_snapshot
from services.city_products import (GENERATION_RULE, HOTEL_LAST_CHECKOUT_OFFSET,
                                    generate_products)
from services.flight_schedule import fictional_duration, great_circle_km
from services.travel import find_hotels


class FictionalScheduleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.airports, cls.flights, cls.offers, cls.report = prepare_snapshot(
            FIXTURE, date(2026, 10, 21))
        cls.airport_map = {item["code"]: item for item in cls.airports}
        cls.flight_map = {item["id"]: item for item in cls.flights}

    def test_cross_continent_arrival_and_fare_are_consistent(self):
        flight = self.flight_map[3831]
        self.assertEqual(flight["flight_no"], "SIM-3831")
        self.assertEqual(flight["departure_at"].astimezone(
            ZoneInfo("Asia/Shanghai")).date(), date(2026, 10, 26))
        self.assertEqual(flight["arrival_at"].astimezone(
            ZoneInfo("Europe/London")).date(), date(2026, 10, 26))
        self.assertGreaterEqual(flight["arrival_at"] - flight["departure_at"],
                                timedelta(hours=10))
        self.assertEqual(next(offer["amount"] for offer in self.offers
                              if offer["flight_id"] == 3831), 6585)

    def test_all_long_routes_have_plausible_intervals(self):
        long_routes = 0
        for flight in self.flights:
            distance = great_circle_km(self.airport_map[flight["departure_airport"]],
                                       self.airport_map[flight["arrival_airport"]])
            interval = flight["arrival_at"] - flight["departure_at"]
            self.assertGreater(interval, timedelta(0))
            if distance >= 5000:
                long_routes += 1
                self.assertGreaterEqual(interval, timedelta(hours=5))
        self.assertGreater(long_routes, 10000)

    def test_corrected_actual_arrival_keeps_source_delay(self):
        with sqlite3.connect(f"file:{FIXTURE.resolve().as_posix()}?mode=ro",
                             uri=True) as db:
            source = db.execute(
                "SELECT scheduled_arrival, actual_arrival FROM flights "
                "WHERE actual_arrival IS NOT NULL ORDER BY flight_id LIMIT 1"
            ).fetchone()
            flight_id = db.execute(
                "SELECT flight_id FROM flights WHERE actual_arrival IS NOT NULL "
                "ORDER BY flight_id LIMIT 1").fetchone()[0]
        old_delay = (datetime.fromisoformat(source[1])
                     - datetime.fromisoformat(source[0]))
        corrected = self.flight_map[flight_id]
        self.assertEqual(corrected["actual_arrival_at"]
                         - corrected["arrival_at"], old_delay)
        self.assertGreater(corrected["actual_arrival_at"],
                           corrected["actual_departure_at"])

    def test_missing_coordinates_and_extremes_do_not_make_false_times(self):
        known = {"latitude": 0.0, "longitude": 0.0}
        for bad in ({"latitude": None, "longitude": 1.0},
                    {"latitude": nan, "longitude": 1.0},
                    {"latitude": 91.0, "longitude": 1.0}):
            with self.assertRaises(ValueError):
                great_circle_km(known, bad)
        self.assertEqual(fictional_duration(0), timedelta(minutes=45))
        self.assertEqual(fictional_duration(25000), timedelta(hours=24))
        with self.assertRaises(ValueError):
            fictional_duration(-1)

    def test_utc_interval_survives_dst_change(self):
        berlin = ZoneInfo("Europe/Berlin")
        departure = datetime(2026, 10, 25, 1, 30, tzinfo=berlin)
        arrival = departure.astimezone(timezone.utc) + timedelta(hours=2)
        self.assertEqual(arrival - departure.astimezone(timezone.utc),
                         timedelta(hours=2))
        self.assertEqual(arrival.astimezone(berlin).hour, 2)
        self.assertEqual(arrival.astimezone(berlin).fold, 1)

    def test_repeated_preparation_keeps_departures_prices_and_arrivals(self):
        _, second_flights, second_offers, _ = prepare_snapshot(
            FIXTURE, date(2026, 10, 21))
        self.assertEqual(self.flights, second_flights)
        self.assertEqual(self.offers, second_offers)

    def test_last_window_arrival_can_find_two_night_hotel(self):
        day = date(2026, 10, 21)
        offered = {row["flight_id"] for row in self.offers}
        latest = max((row for row in self.flights if row["id"] in offered),
                     key=lambda row: row["arrival_at"].astimezone(
                         ZoneInfo(self.airport_map[row["arrival_airport"]]["timezone"]))
                     .date())
        destination = self.airport_map[latest["arrival_airport"]]
        arrival = latest["arrival_at"].astimezone(
            ZoneInfo(destination["timezone"])).date()
        self.assertEqual(arrival, day + timedelta(days=31))
        hotel_rows, room_rows, _ = generate_products(
            self.airports, self.flights, self.offers, day)
        nearby = [row for row in hotel_rows
                  if row["nearby_airport"] == destination["code"]]
        rooms = [row for row in room_rows
                 if row["hotel_id"] in {hotel["id"] for hotel in nearby}]
        self.assertTrue(all(row["generation_rule"] == GENERATION_RULE
                            for row in rooms))
        self.assertGreaterEqual(max(row["available_until"] for row in rooms),
                                day + timedelta(days=HOTEL_LAST_CHECKOUT_OFFSET))
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as db:
            metadata.create_all(db)
            db.execute(insert(demo_state).values(
                id=1, as_of=day, fixture_sha256="a" * 64))
            db.execute(insert(airport_table), [destination])
            db.execute(insert(hotels), nearby)
            db.execute(insert(hotel_products), rooms)
            found = find_hotels(db, destination["city"], arrival,
                                arrival + timedelta(days=2),
                                airport=destination["code"])
            self.assertTrue(found)
            self.assertEqual(find_hotels(db, destination["city"], arrival,
                                         day + timedelta(days=35),
                                         airport=destination["code"]), [])
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
