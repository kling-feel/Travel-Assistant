"""Display labels must cover the shipped airport network without changing codes."""

import sqlite3
import unittest
from pathlib import Path

from services.location_labels import airport_zh, city_zh, product_zh


class LocationLabelTests(unittest.TestCase):
    def test_all_shipped_airports_have_chinese_labels(self):
        source = Path(__file__).resolve().parent.parent / "data/fixtures/network_source.sqlite"
        with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as db:
            rows = db.execute("SELECT airport_code, city FROM airports_data").fetchall()
        self.assertEqual(len(rows), 115)
        self.assertEqual(len({city for _, city in rows}), 106)
        for code, city in rows:
            self.assertRegex(city_zh(city), r"[\u4e00-\u9fff]")
            self.assertRegex(airport_zh(code, city), r"[\u4e00-\u9fff]")
        self.assertEqual(len({airport_zh(code, city) for code, city in rows}), 115)

    def test_generated_product_labels_are_chinese_and_unknown_names_survive(self):
        self.assertEqual(product_zh("虚构·Basel机场周边酒店 1"),
                         "巴塞尔机场周边酒店 · 悦居")
        self.assertEqual(product_zh("虚构·BSL机场租车 2", city="Basel", airport="BSL"),
                         "巴塞尔米卢斯机场租车 · 舒适型")
        self.assertEqual(product_zh("Independent Hotel"), "Independent Hotel")


if __name__ == "__main__":
    unittest.main()
