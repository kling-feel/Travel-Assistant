"""The recommendation city scope follows the imported offered network."""

import unittest
from datetime import date

from sqlalchemy import create_engine, insert

from db.travel_models import airports, flight_offers, flights, metadata
from scripts.import_network_data import FIXTURE, prepare_snapshot
from services.recommendations import reachable_cities


class ReachableCityTests(unittest.TestCase):
    def test_all_offered_destinations_and_only_those_are_in_scope(self):
        airport_rows, flight_rows, offer_rows, _ = prepare_snapshot(
            FIXTURE, date(2026, 10, 17))
        offered = {row["flight_id"] for row in offer_rows}
        offered_flights = [row for row in flight_rows if row["id"] in offered]
        engine = create_engine("sqlite+pysqlite:///:memory:")
        try:
            with engine.begin() as db:
                metadata.create_all(db)
                db.execute(insert(airports), airport_rows)
                db.execute(insert(flights), offered_flights)
                db.execute(insert(flight_offers), offer_rows)
                cities = reachable_cities(db)
                self.assertEqual(len(cities), 95)
                self.assertIn("Basel", cities)
                self.assertEqual(cities, sorted(set(cities), key=str.casefold))
                city_by_code = {row["code"]: row["city"] for row in airport_rows}
                expected = {city_by_code[row["arrival_airport"]]
                            for row in offered_flights if row["status"] == "Scheduled"}
                self.assertEqual(set(cities), expected)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
