"""Regression checks for the fixed fictional itinerary used in the demo."""

import re
import sqlite3
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta

import pytz

from scripts.import_demo_data import DEFAULT_SOURCE, collect_data


class DemoImportTimeTests(unittest.TestCase):
    def test_booked_flight_and_alternative_are_tomorrow_afternoon(self):
        berlin = pytz.timezone("Europe/Berlin")
        for as_of in (date(2026, 10, 17), date(2026, 11, 1)):
            with self.subTest(as_of=as_of):
                with closing(sqlite3.connect(
                    f"file:{DEFAULT_SOURCE.as_posix()}?mode=ro", uri=True
                )) as source:
                    sql, summary = collect_data(source, as_of)

                departures = {}
                for line in sql.splitlines():
                    if line.startswith("INSERT INTO demo_source.flights"):
                        match = re.search(r"VALUES \((\d+), '[^']+', '([^']+)'", line)
                        self.assertIsNotNone(match)
                        departures[int(match.group(1))] = datetime.fromisoformat(match.group(2))

                booked = departures[8017].astimezone(berlin)
                alternative = departures[8014].astimezone(berlin)
                self.assertEqual(booked.date(), as_of + timedelta(days=1))
                self.assertEqual(alternative.date(), booked.date())
                self.assertEqual((booked.hour, alternative.hour), (17, 17))
                self.assertEqual(alternative - booked, timedelta(minutes=15))
                self.assertTrue(any(
                    line.startswith("INSERT INTO demo_source.flight_orders")
                    and ", 8017, " in line
                    for line in sql.splitlines()
                ))
                self.assertEqual((summary["flights"], summary["hotels"], summary["flight_orders"]),
                                 (14, 3, 2))


if __name__ == "__main__":
    unittest.main()
