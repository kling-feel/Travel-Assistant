"""The paid evaluation must stop safely when usage is unavailable."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import p26d_metered_model as meter


class FakeModel:
    def __init__(self, usage):
        self.usage = usage
        self.invocations = 0

    def invoke(self, _messages):
        self.invocations += 1
        return SimpleNamespace(response_metadata={"token_usage": self.usage},
                               usage_metadata={})


class MeteringTests(unittest.TestCase):
    def test_records_cache_split_and_enforces_batch_limit(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
                meter, "LEDGER", Path(directory) / "ledger.json"):
            backend = FakeModel({"prompt_tokens": 1000,
                                 "completion_tokens": 100,
                                 "prompt_cache_hit_tokens": 800,
                                 "prompt_cache_miss_tokens": 200})
            model = meter.BudgetedModel(backend, limit=1)
            model.invoke([])
            row = meter.read_ledger()[0]
            self.assertTrue(row["usage_complete"])
            self.assertEqual(row["upper_rmb"], 0.0028)
            self.assertEqual(row["estimated_rmb"], 0.001232)
            with self.assertRaises(RuntimeError):
                model.invoke([])
            self.assertEqual(backend.invocations, 1)

    def test_missing_usage_stops_next_request(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
                meter, "LEDGER", Path(directory) / "ledger.json"):
            backend = FakeModel({})
            model = meter.BudgetedModel(backend)
            with self.assertRaises(RuntimeError):
                model.invoke([])
            self.assertEqual(len(meter.read_ledger()), 1)
            with self.assertRaises(RuntimeError):
                model.invoke([])
            self.assertEqual(backend.invocations, 1)


if __name__ == "__main__":
    unittest.main()
