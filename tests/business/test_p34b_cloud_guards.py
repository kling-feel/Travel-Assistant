"""Cloud-only controls must not create orders or disclose credentials."""

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select

from scripts.container_target import PUBLIC_DOMAIN
from scripts.manage_travelers import create, disable, disable_seeds, rotate
from travel_assistant.core.password_hash import verify_password
from travel_assistant.core.request_limits import RequestLimits
from travel_assistant.db.travel_models import metadata, travelers
from travel_assistant.services.answer_composer import ComposingModel, compose_verified_answer
from travel_assistant.services.provider_usage import ProviderBalanceError, classify_error


class CloudGuardTests(unittest.TestCase):
    def test_public_domain_rejects_malformed_hosts(self):
        self.assertTrue(PUBLIC_DOMAIN.fullmatch("travel.example.com"))
        for host in ("travel..example.com", "-travel.example.com", "travel.example.com:443",
                     "localhost", "travel_example.com"):
            self.assertIsNone(PUBLIC_DOMAIN.fullmatch(host))

    def test_login_failure_and_turn_limits(self):
        now = [1000.0]
        guard = RequestLimits(clock=lambda: now[0])
        for _ in range(8):
            self.assertTrue(guard.allow_login("Guest"))
            guard.login_result("Guest", False)
        self.assertFalse(guard.allow_login("guest"))
        now[0] += 901
        self.assertTrue(guard.allow_login("guest"))
        guard.login_result("guest", True)
        self.assertTrue(guard.acquire_turn("owner"))
        self.assertTrue(guard.acquire_turn("owner"))
        self.assertFalse(guard.acquire_turn("owner"))
        guard.release_turn("owner")
        guard.release_turn("owner")
        for _ in range(18):
            self.assertTrue(guard.acquire_turn("owner"))
            guard.release_turn("owner")
        self.assertFalse(guard.acquire_turn("owner"))
        now[0] += 61
        self.assertTrue(guard.acquire_turn("owner"))
        guard.release_turn("owner")

    def test_account_provision_rotate_and_disable(self):
        database = create_engine("sqlite+pysqlite:///:memory:")
        metadata.create_all(database, tables=[travelers])
        with database.begin() as connection:
            secret = create(connection, "reviewer1", "面试访客")
            row = connection.execute(select(travelers).where(
                travelers.c.username == "reviewer1")).mappings().one()
            self.assertTrue(verify_password(secret["password"], row["password_hash"]))
            renewed = rotate(connection, "reviewer1")
            self.assertNotEqual(secret["password"], renewed["password"])
            disable_seeds(connection)
            disable(connection, "reviewer1")
            self.assertEqual(connection.execute(select(travelers.c.active).where(
                travelers.c.username == "reviewer1")).scalar_one(), 0)
            with self.assertRaises(ValueError):
                rotate(connection, "reviewer1")

    def test_model_usage_has_safe_balance_category(self):
        class PaymentError(Exception):
            status_code = 402

        class UnpaidBackend:
            def invoke(self, _messages):
                raise PaymentError("private upstream details")

        self.assertEqual(classify_error(PaymentError()), "balance")
        with patch("travel_assistant.services.provider_usage.record") as usage:
            with self.assertRaises(ProviderBalanceError) as raised:
                ComposingModel(UnpaidBackend()).invoke([])
        self.assertNotIn("private upstream details", str(raised.exception))
        usage.assert_called_once_with("model", "balance")

    def test_optional_presentation_balance_keeps_verified_fact_and_warns(self):
        class UnpaidBackend:
            def invoke(self, _messages):
                raise ProviderBalanceError("balance")

        answer = compose_verified_answer(
            ComposingModel(UnpaidBackend()), "flight_search", "查机票",
            "找到以下航班：班次甲；班次乙",
            [{"type": "flight"}, {"type": "flight"}],
        )
        self.assertIn("班次甲", answer)
        self.assertIn("余额不足", answer)


if __name__ == "__main__":
    unittest.main()
