"""Paid acceptance runner must stop before its call limits."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import run_p31h3_metered as meter


class _Backend:
    def invoke(self, _messages):
        return SimpleNamespace(response_metadata={"token_usage": {
            "prompt_tokens": 100, "completion_tokens": 50}})


class _MissingUsage(_Backend):
    calls = 0

    def invoke(self, _messages):
        self.calls += 1
        return SimpleNamespace(response_metadata={"token_usage": {}}, usage_metadata={})


class _TimeoutThenSuccess(_Backend):
    calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            raise APITimeoutError("test timeout")
        return super().invoke(messages)


class APITimeoutError(Exception):
    pass


class MeterTests(unittest.TestCase):
    def test_missing_usage_records_failure_and_stops_next_paid_request(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
                meter, "LEDGER", Path(folder) / "ledger.json"), patch(
                    "scripts.run_p31h3_metered.get_chat_model",
                    return_value=SimpleNamespace(backend=_MissingUsage())):
            model = meter.CountedModel()
            with self.assertRaisesRegex(RuntimeError, "usage missing"):
                model.invoke([])
            self.assertEqual(meter._rows()[0]["status"], "usage_missing")
            with self.assertRaisesRegex(RuntimeError, "limit reached"):
                model.invoke([])
            self.assertEqual(model.backend.calls, 1)
            self.assertEqual(len(meter._rows()), 1)

    def test_model_and_search_hard_stops(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
                meter, "LEDGER", Path(folder) / "ledger.json"), patch.object(
                    meter, "MODEL_CAP", 2), patch.object(meter, "SEARCH_CAP", 1), patch(
                        "scripts.run_p31h3_metered.get_chat_model",
                        return_value=SimpleNamespace(backend=_Backend())):
            model = meter.CountedModel()
            model.invoke([])
            model.invoke([])
            with self.assertRaisesRegex(RuntimeError, "limit reached"):
                model.invoke([])
            search = meter._count_search(lambda: [], "place")
            self.assertEqual(search(), [])
            with self.assertRaisesRegex(RuntimeError, "limit reached"):
                search()
            rows = meter._rows()
            self.assertEqual([row["kind"] for row in rows],
                             ["model", "model", "search"])
            self.assertTrue(all(row["status"] == "ok" for row in rows))

    def test_explicit_timeout_reserves_budget_and_counts_attempt(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
                meter, "LEDGER", Path(folder) / "ledger.json"), patch.object(
                    meter, "MODEL_CAP", 2), patch.object(
                        meter, "RETRYABLE_FAILURES", frozenset({"APITimeoutError"})), patch.object(
                            meter, "UNKNOWN_RMB_RESERVE", 0.5), patch(
                                "scripts.run_p31h3_metered.get_chat_model",
                                return_value=SimpleNamespace(backend=_TimeoutThenSuccess())):
            model = meter.CountedModel()
            with self.assertRaises(APITimeoutError):
                model.invoke([])
            model.invoke([])
            with self.assertRaisesRegex(RuntimeError, "limit reached"):
                model.invoke([])
            self.assertEqual([row["status"] for row in meter._rows()],
                             ["APITimeoutError", "ok"])
            self.assertEqual(meter._rows()[0]["upper_rmb"], 0.5)


if __name__ == "__main__":
    unittest.main()
