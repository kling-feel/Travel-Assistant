"""Full-city catalog and high-risk city routing stay inside reviewed facts."""

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import insert
from sqlalchemy.orm import Session

from db.travel_models import airports, flight_offers, flights
from services.recommendation_geography import airport_geography
from services.recommendations import ReviewedCatalogProvider, read_recommendation
from services.reviewed_places import places_for_city, reviewed_places


class FullCatalogTests(unittest.TestCase):
    def test_previous_version_is_unchanged_and_unreachable_city_is_excluded(self):
        root = Path(__file__).resolve().parents[1] / "data/fixtures"
        old = (root / "reviewed_places_v1.psv").read_text(encoding="utf-8").splitlines()
        current = (root / "reviewed_places_v2.psv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(current[:len(old)], old)
        self.assertEqual(places_for_city("Berlin", "DE"), [])
        self.assertEqual(places_for_city("Victoria", "CA"), [])
        self.assertEqual(len(places_for_city("Victoria", "SC")), 2)
        self.assertEqual(len(places_for_city("Newark", "US")), 2)

        with TemporaryDirectory() as directory:
            tampered = Path(directory) / "tampered.psv"
            tampered.write_text("\n".join(current).replace(
                "basel-minster|", "basel-other|", 1) + "\n", encoding="utf-8")
            reviewed_places.cache_clear()
            with patch("services.reviewed_places._SOURCE", tampered):
                with self.assertRaises(ValueError):
                    reviewed_places()
        reviewed_places.cache_clear()

    def test_recommendations_for_risk_cities_use_matching_country_and_no_cards(self):
        from tests.test_p26a_dialogue import DialogueTests

        fixture = DialogueTests("runTest")
        fixture.setUp()
        try:
            cases = (
                ("EWR", "Newark", "推荐纽瓦克的经典地点", "US"),
                ("NGO", "Nagoya", "推荐名古屋的经典地点", "JP"),
                ("PPT", "Papeete", "推荐帕皮提的经典地点", "PF"),
                ("SEZ", "Victoria", "推荐塞舌尔维多利亚的经典地点", "SC"),
                ("DCA", "Washington, D.C.", "推荐华盛顿的经典地点", "US"),
            )
            geography = airport_geography()
            with fixture.engine.begin() as db:
                for index, (code, city, _, _) in enumerate(cases, start=20):
                    db.execute(insert(airports).values(
                        code=code, name=f"Fictional {city}", city=city,
                        timezone=geography[code]["timezone"]))
                    db.execute(insert(flights).values(
                        id=index, flight_no=f"SIM-{index}",
                        departure_airport="HAM", arrival_airport=code,
                        status="Scheduled",
                        departure_at=datetime(2026, 10, 21, 9, tzinfo=timezone.utc),
                        arrival_at=datetime(2026, 10, 21, 12, tzinfo=timezone.utc)))
                    db.execute(insert(flight_offers).values(
                        flight_id=index, amount=200 + index, version=1,
                        rule_id="network-fare-v1"))
            with Session(fixture.engine) as db:
                for _, city, prompt, country in cases:
                    with self.subTest(city=city):
                        result = read_recommendation(
                            db, "t1", prompt, {"city": city}, ReviewedCatalogProvider())
                        refs = result["references"]
                        self.assertEqual(len(refs), 2)
                        self.assertEqual({ref["city"] for ref in refs}, {city})
                        self.assertEqual({ref["country_code"] for ref in refs}, {country})
                        self.assertTrue(any(ref["place"] in result["answer"] for ref in refs))
                        self.assertNotIn("https://", result["answer"])
                outside = read_recommendation(
                    db, "t1", "推荐 Toronto 的景点", {"city": "Toronto"},
                    ReviewedCatalogProvider())
                self.assertEqual(outside["references"], [])
                self.assertIn("不在当前航班可达城市", outside["answer"])
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
