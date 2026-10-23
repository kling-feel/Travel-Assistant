"""Run the isolated cancellation page under its own paid-call budget."""

from scripts import run_p31h3_metered as meter


meter.LEDGER = meter.ROOT / "data/generated/p31h4a-paid-ledger.json"
meter.MODEL_CAP = 30
meter.SEARCH_CAP = 0  # Cancellation has no reason to query external pages.
meter.MODEL_RMB_CAP = 8.0
meter.RETRYABLE_FAILURES = frozenset({"APITimeoutError"})
meter.UNKNOWN_RMB_RESERVE = 0.5


if __name__ == "__main__":
    meter.main()
