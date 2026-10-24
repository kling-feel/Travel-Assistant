"""A destination named by the traveler resolves one owned future arrival."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from travel_assistant.services.agent_reads import read_for_assistant


class LandingSelectionTests(unittest.TestCase):
    def test_city_filters_two_confirmed_flights_without_guessing(self):
        future = datetime.now(timezone.utc) + timedelta(days=3)
        orders = {"flight_orders": [
            {"status": "confirmed", "arrival_at": future, "arrival_airport": "BSL",
             "flight_id": 1},
            {"status": "confirmed", "arrival_at": future, "arrival_airport": "LGW",
             "flight_id": 2},
        ]}
        places = {"BSL": ("巴塞尔机场", "Europe/Zurich", "Basel"),
                  "LGW": ("伦敦盖特威克机场", "Europe/London", "London")}
        with patch("travel_assistant.services.agent_reads.itinerary", return_value=orders), \
                patch("travel_assistant.services.agent_reads._airport",
                      side_effect=lambda _db, code: places[code]):
            ambiguous = read_for_assistant(None, "traveler", "landing_context", {})
            london = read_for_assistant(None, "traveler", "landing_context",
                                        {"city": "London"})
        self.assertEqual(ambiguous["references"], [])
        self.assertEqual([row["id"] for row in london["references"]], [2])


if __name__ == "__main__":
    unittest.main()
