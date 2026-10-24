"""The reviewed catalog is bounded by geography, evidence age and source type."""

import unittest
from collections import Counter
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from services.recommendation_geography import country_for_fixture_city
from services.reviewed_places import places_for_city, reviewed_places
from services.recommendations import _CITY_ALIASES, _explicit_city
from scripts.verify_network_fixture import reachable_cities


class ReviewedPlacesTests(unittest.TestCase):
    def test_coverage_and_geography(self):
        rows = reviewed_places()
        self.assertEqual(len(rows), 190)
        self.assertEqual({row["city"] for row in rows}, set(reachable_cities()))
        self.assertEqual(set(Counter(row["city"] for row in rows).values()), {2})
        self.assertEqual(len({row["id"] for row in rows}), 190)
        self.assertEqual(len({row["url"] for row in rows}), 190)
        self.assertEqual(set(_CITY_ALIASES.values()), set(reachable_cities()))
        for row in rows:
            self.assertEqual(country_for_fixture_city(row["city"])["code"],
                             row["country_code"])
            self.assertIn(row["reviewed_on"], {"2026-10-19", "2026-10-20"})

    def test_country_theme_and_age_fail_closed(self):
        self.assertEqual(len(places_for_city("Basel", "CH")), 2)
        self.assertEqual(places_for_city("Basel", "FR"), [])
        self.assertEqual(places_for_city("Paris", "US"), [])
        self.assertEqual(len(places_for_city("Basel", "CH", "architecture")), 1)
        self.assertEqual(places_for_city("Basel", "CH", "nature"), [])
        self.assertEqual(places_for_city("Basel", "CH", on=date(2027, 4, 18)), [])
        for alias, city in (("首尔", "Seoul"), ("香港", "Hong Kong"),
                            ("圣保罗", "São Paulo"), ("内罗毕", "Nairobi")):
            self.assertEqual(_explicit_city(f"推荐{alias}的景点", [city]), city)

    def test_catalog_read_error_does_not_become_model_facts(self):
        reviewed_places.cache_clear()
        with patch("services.reviewed_places._SOURCE") as source:
            source.open.side_effect = OSError("catalog missing")
            with self.assertRaises(OSError):
                reviewed_places()
        reviewed_places.cache_clear()
        self.assertEqual(len(reviewed_places()), 190)

    def test_official_label_cannot_be_spoofed_by_an_unlisted_host(self):
        source = Path(__file__).resolve().parents[1] / "data/fixtures/reviewed_places_v2.psv"
        original = source.read_text(encoding="utf-8")
        forged = original.replace(
            "https://www.basel.com/en/attractions/basler-muenster-bc9d869a33",
            "https://evil.example/official")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "forged.psv"
            path.write_text(forged, encoding="utf-8")
            reviewed_places.cache_clear()
            with patch("services.reviewed_places._SOURCE", path):
                with self.assertRaises(ValueError):
                    reviewed_places()
        reviewed_places.cache_clear()


if __name__ == "__main__":
    unittest.main()
