"""Keep current fixture timing and reset guards when retiring the small fixture."""

import unittest
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from scripts.import_network_data import FIXTURE, _formal_reset, prepare_snapshot


class NetworkFixtureTimingTests(unittest.TestCase):
    def test_demo_route_keeps_local_afternoon_gap_across_dst(self):
        previous_fares = None
        for as_of, offset_hours in ((date(2026, 10, 17), 2),
                                    (date(2026, 11, 1), 1)):
            with self.subTest(as_of=as_of):
                _, flights, offers, _ = prepare_snapshot(FIXTURE, as_of)
                by_id = {flight["id"]: flight for flight in flights}
                berlin = ZoneInfo("Europe/Berlin")
                first = by_id[8144]["departure_at"].astimezone(berlin)
                second = by_id[8152]["departure_at"].astimezone(berlin)
                self.assertEqual(first.date(), as_of + timedelta(days=1))
                self.assertEqual(second.date(), first.date())
                self.assertEqual((first.hour, first.minute), (17, 43))
                self.assertEqual((second.hour, second.minute), (17, 58))
                self.assertEqual(second - first, timedelta(minutes=15))
                self.assertEqual(first.utcoffset(), timedelta(hours=offset_hours))
                fares = {row["flight_id"]: row["amount"] for row in offers}
                current_fares = (fares[8144], fares[8152])
                if previous_fares is not None:
                    self.assertEqual(current_fares, previous_fares)
                previous_fares = current_fares


class ResetGuardTests(unittest.TestCase):
    def _database(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as db:
            db.exec_driver_sql("CREATE TABLE travelers (username TEXT NOT NULL)")
            db.exec_driver_sql("CREATE TABLE pending_actions (status TEXT NOT NULL)")
            db.exec_driver_sql("CREATE TABLE marker (id INTEGER NOT NULL)")
            db.exec_driver_sql("INSERT INTO marker VALUES (1)")
        return engine

    def test_non_demo_account_blocks_reset_before_any_delete(self):
        engine = self._database()
        with engine.begin() as db:
            db.exec_driver_sql("INSERT INTO travelers VALUES ('demo1'), ('other')")
            with self.assertRaisesRegex(RuntimeError, "Non-demo account"):
                _formal_reset(db, date(2026, 10, 24), {}, "unused", ())
            self.assertEqual(db.execute(text("SELECT id FROM marker")).scalar_one(), 1)
            self.assertEqual(db.execute(text("SELECT COUNT(*) FROM travelers")).scalar_one(), 2)
        engine.dispose()

    def test_pending_action_blocks_reset_before_any_delete(self):
        engine = self._database()
        with engine.begin() as db:
            db.exec_driver_sql("INSERT INTO travelers VALUES ('demo1')")
            db.exec_driver_sql("INSERT INTO pending_actions VALUES ('pending')")
            with self.assertRaisesRegex(RuntimeError, "Pending actions"):
                _formal_reset(db, date(2026, 10, 24), {}, "unused", ())
            self.assertEqual(db.execute(text("SELECT id FROM marker")).scalar_one(), 1)
            self.assertEqual(db.execute(text("SELECT status FROM pending_actions")).scalar_one(),
                             "pending")
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
