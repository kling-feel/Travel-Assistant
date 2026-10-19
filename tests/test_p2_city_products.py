"""Reachable supply, bounded reads and audited fictional policies."""

import unittest
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import create_engine, insert, inspect, select, update

from db.travel_models import (airports, demo_state, hotel_products, hotels, metadata,
                              policy_documents, rental_products)
from migrations import v007_city_products
from scripts.import_network_data import FIXTURE, prepare_snapshot
from services.city_products import GENERATION_RULE, generate_products
from services.policy_catalog import ALL_POLICIES, CURRENT_POLICIES, SEED_POLICIES
from services.travel import OrderError, find_hotels, find_rentals, policies
from tools.retriever_vector import lookup_policy


class CityProductTests(unittest.TestCase):
    def test_all_reachable_airports_have_stable_products_on_two_dates(self):
        snapshots = []
        for day in (date(2026, 10, 17), date(2026, 11, 1)):
            airport_rows, flight_rows, offer_rows, _ = prepare_snapshot(FIXTURE, day)
            hotels_data, rooms, rentals = generate_products(
                airport_rows, flight_rows, offer_rows, day)
            self.assertEqual((len(hotels_data), len(rooms), len(rentals)), (312, 312, 208))
            self.assertEqual(len({row["nearby_airport"] for row in hotels_data}), 104)
            self.assertEqual(len({row["city"] for row in hotels_data}), 95)
            self.assertEqual(len({row["airport_code"] for row in rentals}), 104)
            self.assertEqual(len({row["id"] for row in hotels_data}), 312)
            self.assertEqual(len({row["id"] for row in rooms}), 312)
            self.assertEqual(len({row["id"] for row in rentals}), 208)
            self.assertEqual([row["id"] for row in rooms if row["hotel_id"] in (1, 2, 3)],
                             ["room-1", "room-2", "room-3"])
            self.assertTrue(all(row["generation_rule"] == GENERATION_RULE
                                for row in rooms + rentals))
            snapshots.append((hotels_data, rooms, rentals))
        for index in (1, 2):
            self.assertEqual([row["id"] for row in snapshots[0][index]],
                             [row["id"] for row in snapshots[1][index]])
            price_key = "nightly_price" if index == 1 else "daily_price"
            self.assertEqual([row[price_key] for row in snapshots[0][index]],
                             [row[price_key] for row in snapshots[1][index]])

    def test_v007_upgrades_existing_tables_without_rewriting_policy_history(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as db:
            for sql in (
                "CREATE TABLE airports (code varchar(8) PRIMARY KEY)",
                "CREATE TABLE hotels (id integer PRIMARY KEY, city varchar(80))",
                "CREATE TABLE hotel_products (id varchar(80) PRIMARY KEY, "
                "available_from date, available_until date)",
                "CREATE TABLE policy_documents (id varchar(80) PRIMARY KEY, version integer, "
                "title varchar(120), scope varchar(80), body text)",
            ):
                db.exec_driver_sql(sql)
            db.exec_driver_sql("INSERT INTO policy_documents VALUES "
                               "('old-rule',1,'old','hotel','historical')")
            v007_city_products.upgrade(db)
            v007_city_products.upgrade(db)
            self.assertTrue({"nearby_airport", "latitude", "longitude"} <=
                            {column["name"] for column in inspect(db).get_columns("hotels")})
            self.assertIn("generation_rule", {column["name"] for column in
                                              inspect(db).get_columns("hotel_products")})
            self.assertTrue(inspect(db).has_table("rental_products"))
            self.assertEqual(db.execute(select(policy_documents.c.body).where(
                policy_documents.c.id == "old-rule")).scalar_one(), "historical")
            self.assertEqual(len(db.execute(select(policy_documents)).all()), 8)
        engine.dispose()

    def test_city_search_dates_airport_and_policy_source(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        day = date(2026, 10, 17)
        with engine.begin() as db:
            metadata.create_all(db)
            db.execute(insert(demo_state).values(id=1, as_of=day, fixture_sha256="a" * 64))
            db.execute(insert(airports), [
                dict(code="LHR", name="Heathrow", city="London", timezone="Europe/London",
                     latitude=51.47, longitude=-0.45),
                dict(code="LGW", name="Gatwick", city="London", timezone="Europe/London",
                     latitude=51.15, longitude=-0.18),
            ])
            db.execute(insert(hotels), [
                dict(id=1, name="虚构酒店 A", city="London", nearby_airport="LHR",
                     latitude=51.48, longitude=-0.45),
                dict(id=2, name="虚构酒店 B", city="London", nearby_airport="LGW",
                     latitude=51.16, longitude=-0.18),
            ])
            db.execute(insert(hotel_products), [
                dict(id=f"room-{code}", hotel_id=number, room_type="模拟房型",
                     nightly_price=price, available_from=day + timedelta(days=1),
                     available_until=day + timedelta(days=31))
                for code, number, price in (("LHR", 1, 150), ("LGW", 2, 90))
            ])
            db.execute(insert(rental_products), [
                dict(id=f"rental-{code}", airport_code=code, city="London",
                     name="虚构租车", vehicle_type="模拟经济型", daily_price=price,
                     latitude=lat, longitude=lon,
                     available_from=day + timedelta(days=1),
                     available_until=day + timedelta(days=31),
                     generation_rule=GENERATION_RULE)
                for code, price, lat, lon in (("LHR", 100, 51.47, -0.45),
                                              ("LGW", 80, 51.15, -0.18))
            ])
            db.execute(insert(policy_documents), list(SEED_POLICIES))
            check_in, check_out = day + timedelta(days=1), day + timedelta(days=2)
            self.assertEqual([row["product_id"] for row in find_hotels(
                db, "London", check_in, check_out)], ["room-LGW", "room-LHR"])
            nearby = find_hotels(db, "London", check_in, check_out,
                                 airport="LHR", sort="distance")
            self.assertEqual([row["product_id"] for row in nearby], ["room-LHR"])
            self.assertLess(nearby[0]["distance_km"], 2)
            self.assertEqual(find_hotels(db, "London", check_in, check_out,
                                         limit=1, offset=1)[0]["product_id"], "room-LHR")
            self.assertEqual(find_hotels(db, "London", day, check_out), [])
            self.assertEqual(find_hotels(db, "Unknown", check_in, check_out), [])
            self.assertEqual(find_hotels(db, "London", check_in, check_out,
                                         airport="XXX"), [])
            self.assertEqual([row["id"] for row in find_rentals(
                db, check_in, check_out, city="London", max_daily_price=90)],
                ["rental-LGW"])
            self.assertEqual([row["id"] for row in find_rentals(
                db, check_in, check_out, airport="LHR", city="London")],
                ["rental-LHR"])
            self.assertEqual(find_rentals(db, day, check_out, city="London"), [])
            with self.assertRaises(OrderError):
                find_rentals(db, check_out, check_in, city="London")
            self.assertEqual([row["id"] for row in policies(db, "flight")],
                             [CURRENT_POLICIES["flight"]["id"]])
            self.assertEqual([row["id"] for row in policies(db, "rental")],
                             [CURRENT_POLICIES["rental"]["id"]])
            db.execute(update(policy_documents).where(
                policy_documents.c.id == CURRENT_POLICIES["hotel"]["id"]
            ).values(body="unreviewed change"))
            with self.assertRaises(OrderError):
                policies(db, "hotel")
        text = Path("data/policies/fictional_platform.md").read_text(encoding="utf-8")
        for rule in CURRENT_POLICIES.values():
            self.assertIn(rule["id"], text)
            self.assertIn(rule["body"], text)
        self.assertIn("flight-actions-demo-v3", lookup_policy.invoke("机票取消政策"))
        self.assertNotIn("order_faq.md", lookup_policy.invoke("酒店政策"))
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
