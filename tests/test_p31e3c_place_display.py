"""Chinese place labels keep their source identity and verified theme."""

import unittest
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import insert
from sqlalchemy.orm import Session

from db.travel_models import airports, flight_offers, flights
from services.recommendations import ReviewedCatalogProvider, read_recommendation
from services.reviewed_places import places_for_city, reviewed_places


class PlaceDisplayTests(unittest.TestCase):
    def test_overlay_is_complete_and_yu_garden_is_cultural(self):
        places = reviewed_places()
        self.assertEqual(len(places), 190)
        self.assertEqual(len(Counter(row["city"] for row in places)), 95)
        self.assertTrue(all(re.search(r"[\u4e00-\u9fff]", row["display_name_zh"])
                            for row in places))
        garden = next(row for row in places if row["id"] == "shanghai-yu-garden")
        self.assertEqual((garden["name"], garden["display_name_zh"]),
                         ("Yu Garden", "豫园"))
        self.assertEqual((garden["original_category"], garden["category"]),
                         ("nature", "culture"))
        self.assertEqual({row["id"] for row in places_for_city("Shanghai", "CN", "culture")},
                         {"shanghai-yu-garden"})
        self.assertEqual(places_for_city("Shanghai", "CN", "nature"), [])

    def test_tampered_display_fails_closed(self):
        source = Path(__file__).resolve().parents[1] / "data/fixtures/reviewed_place_display_v1.psv"
        content = source.read_text(encoding="utf-8")
        with TemporaryDirectory() as directory:
            tampered = Path(directory) / "tampered.psv"
            tampered.write_text(content.replace("basel-minster|", "wrong-place|", 1),
                                encoding="utf-8")
            reviewed_places.cache_clear()
            try:
                with patch("services.reviewed_places._DISPLAY_SOURCE", tampered):
                    with self.assertRaises(ValueError):
                        reviewed_places()
            finally:
                reviewed_places.cache_clear()

    def test_answer_and_reference_share_place_id(self):
        from tests.test_p26a_dialogue import DialogueTests

        fixture = DialogueTests("runTest")
        fixture.setUp()
        try:
            with fixture.engine.begin() as db:
                db.execute(insert(airports).values(
                    code="SHA", name="Fictional Shanghai", city="Shanghai",
                    timezone="Asia/Shanghai"))
                db.execute(insert(flights).values(
                    id=318, flight_no="SIM-318", departure_airport="HAM",
                    arrival_airport="SHA", status="Scheduled",
                    departure_at=datetime(2026, 10, 22, 9, tzinfo=timezone.utc),
                    arrival_at=datetime(2026, 10, 22, 12, tzinfo=timezone.utc)))
                db.execute(insert(flight_offers).values(
                    flight_id=318, amount=200, version=1,
                    rule_id="network-fare-v1"))
            with Session(fixture.engine) as db:
                answer = read_recommendation(db, "t1", "推荐上海的文化地点",
                                             {"city": "Shanghai"},
                                             ReviewedCatalogProvider())
                follow_up = read_recommendation(db, "t1", "豫园适合文化游吗？", {},
                                                ReviewedCatalogProvider())
            self.assertIn("豫园（Yu Garden）", answer["answer"])
            self.assertIn("文化偏好", answer["answer"])
            self.assertNotIn("自然主题", answer["answer"])
            self.assertEqual(len(answer["references"]), 1)
            ref = answer["references"][0]
            self.assertEqual((ref["id"], ref["display_name"], ref["place"]),
                             ("shanghai-yu-garden", "豫园", "Yu Garden"))
            self.assertIn("豫园（Yu Garden）", follow_up["answer"])
            self.assertEqual(follow_up["references"][0]["id"], "shanghai-yu-garden")
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
