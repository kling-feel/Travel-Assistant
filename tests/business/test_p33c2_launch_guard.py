"""The live launcher must reject a misdirected or changed demo database."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import URL

from scripts.run_p33c2_live import EXPECTED_FIXTURE, EXPECTED_SUPPLY, checked_batch


class LiveLaunchGuardTests(unittest.TestCase):
    def setUp(self):
        self.report = {"as_of": "2026-10-24", "fixture_sha256": EXPECTED_FIXTURE,
                       "supply_digest": EXPECTED_SUPPLY,
                       "migration_versions": list(range(13)),
                       "non_demo_accounts": 0, "counts": {"travelers": 2}}
        self.engine = SimpleNamespace(
            url=URL.create("postgresql+psycopg", username="travel_app", password="hidden",
                           host="127.0.0.1", port=5436, database="travel_demo"),
            connect=MagicMock())
        self.settings = SimpleNamespace(HOST="127.0.0.1", PORT=8000,
                                        POSTGRES_PORT=5436, DATABASE_URL="",
                                        AI_PROVIDER="deepseek", AI_API_KEY="configured",
                                        ALIYUN_IQS_ENABLED=True,
                                        ALIYUN_IQS_API_KEY="configured")

    def test_accepts_only_expected_batch_without_writing(self):
        with patch("scripts.run_p33c2_live.engine", self.engine), \
                patch("scripts.run_p33c2_live.settings", self.settings), \
                patch("scripts.run_p33c2_live.summarize", return_value=self.report) as read:
            self.assertEqual(checked_batch(), self.report)
        read.assert_called_once()
        self.engine.connect.assert_called_once()

    def test_wrong_port_fails_before_database_access(self):
        self.engine.url = self.engine.url.set(port=5433)
        with patch("scripts.run_p33c2_live.engine", self.engine), \
                patch("scripts.run_p33c2_live.settings", self.settings):
            with self.assertRaisesRegex(RuntimeError, "5436"):
                checked_batch()
        self.engine.connect.assert_not_called()

    def test_changed_supply_blocks_start(self):
        self.report["supply_digest"] = "changed"
        with patch("scripts.run_p33c2_live.engine", self.engine), \
                patch("scripts.run_p33c2_live.settings", self.settings), \
                patch("scripts.run_p33c2_live.summarize", return_value=self.report):
            with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                checked_batch()


if __name__ == "__main__":
    unittest.main()
