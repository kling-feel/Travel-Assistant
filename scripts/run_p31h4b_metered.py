"""Run the isolated recovery page under its own paid-call budget."""

from scripts import run_p31h3_metered as meter


meter.LEDGER = meter.ROOT / "data/generated/p31h4b-paid-ledger.json"
meter.MODEL_CAP = 10
meter.SEARCH_CAP = 0
meter.MODEL_RMB_CAP = 8.0
meter.RETRYABLE_FAILURES = frozenset({"APITimeoutError"})
meter.UNKNOWN_RMB_RESERVE = 0.5


if __name__ == "__main__":
    meter.main()
