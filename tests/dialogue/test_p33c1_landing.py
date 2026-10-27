"""A destination named by the traveler resolves one owned future arrival."""

import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver

from travel_assistant.agents.controlled import build_graph
from travel_assistant.services.agent_reads import read_for_assistant
from tests.dialogue.test_p26a_dialogue import ScriptedModel


class LandingSelectionTests(unittest.TestCase):
    def test_earlier_rental_pickup_requires_reconciliation_then_scopes_airport(self):
        calls = []

        def evidence(intent, params):
            calls.append((intent, dict(params)))
            return {"answer": "希思罗机场可用车辆", "references": []}

        graph = build_graph(ScriptedModel(), evidence, InMemorySaver())
        task = {"intent": "hotel_search", "city": "London", "airport": "LHR",
                "_landing_flight_id": 18319, "suggested_check_in": "2026-11-02"}
        base = {"demo_date": "2026-10-27", "task_contexts": {"hotel_search": task},
                "location_context": {"current_city": "Auckland"}, "answer": "",
                "references": [], "transaction_plan": []}
        with patch("travel_assistant.agents.controlled.local_today",
                   return_value=date(2026, 10, 27)):
            early = graph.invoke({**base, "history": [{"role": "user", "content":
                "伦敦机场附近租车，10月31日18点取，11月2日18点还"}]},
                {"configurable": {"thread_id": "early-rental"}})
            later = graph.invoke({**base, "history": [{"role": "user", "content":
                "伦敦机场附近租车，11月2日18点取，11月4日18点还"}]},
                {"configurable": {"thread_id": "later-rental"}})
        self.assertIn("早于落地日期", early["answer"])
        self.assertEqual(early["intent"], "general")
        self.assertEqual(later["intent"], "rental_search")
        self.assertEqual(calls[-1][1]["airport"], "LHR")

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
