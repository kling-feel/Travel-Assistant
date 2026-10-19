"""Run a bounded, private CleverSee source review without exposing API credentials."""

import argparse
import json
import time
from pathlib import Path

from config import settings
from services.recommendations import (CleverSeeRecommendationProvider,
                                      RecommendationSearchError)


CASES = (
    ("Basel", "architecture"),
    ("Tokyo", "culture"),
    ("Paris", "history"),
    ("Dubai", "architecture"),
    ("New York", "nature"),
    ("Beijing", "history"),
    ("Bangkok", "food"),
    ("Rome", "history"),
)
FOLLOWUP_CASES = (("Dubai", "culture"), ("New York", "history"),
                  ("Bangkok", "culture"), ("Basel", "history"),
                  ("Tokyo", None))
LEDGER = Path(__file__).resolve().parent.parent / "data/generated/p27b-budget-ledger.json"
LIMIT = 25


def save(ledger: dict) -> None:
    temporary = LEDGER.with_suffix(".tmp")
    temporary.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(LEDGER)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-new", type=int, default=1)
    parser.add_argument("--case", choices=[f"{city}:{preference or 'none'}"
                                           for city, preference in (*CASES, *FOLLOWUP_CASES)])
    args = parser.parse_args()
    if not 1 <= args.max_new <= len(CASES):
        parser.error("--max-new must be between 1 and 8")
    if not settings.ALIYUN_IQS_API_KEY.strip():
        parser.error("local search key is not configured")
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    ledger["note"] = ("User authorized live GlobalBasic evaluation without a visible unit price. "
                      "Limit total search requests to 25; actual cost must be read from billing. "
                      "The first connectivity call found a false positive and is included in the total.")
    samples = ledger.setdefault("source_samples", [])
    if len(samples) >= len(CASES) and not args.case:
        print("All planned source cases have already run.")
        return
    cases = ([next(case for case in (*CASES, *FOLLOWUP_CASES)
                   if f"{case[0]}:{case[1] or 'none'}" == args.case)]
             if args.case else CASES[len(samples):len(samples) + args.max_new])
    for city, preference in cases:
        if ledger["search_requests"] >= LIMIT:
            print("Search request count limit reached.")
            break
        # Reserve a call before networking so an interrupted run cannot undercount.
        ledger["search_requests"] += 1
        save(ledger)
        started = time.monotonic()
        diagnostics = []
        provider = CleverSeeRecommendationProvider(
            settings.ALIYUN_IQS_API_KEY.strip(), diagnostics=diagnostics)
        try:
            rows = provider.search(city, preference)
            outcome = {"city": city, "preference": preference, "error": None,
                       "accepted": rows, "diagnostics": diagnostics}
        except RecommendationSearchError as exc:
            outcome = {"city": city, "preference": preference, "error": exc.kind,
                       "accepted": [], "diagnostics": diagnostics}
        outcome["elapsed_seconds"] = round(time.monotonic() - started, 2)
        if args.case:
            ledger.setdefault("followup_samples", []).append(outcome)
        else:
            samples.append(outcome)
        save(ledger)
        print(json.dumps({"city": city, "preference": preference,
                          "elapsed_seconds": outcome["elapsed_seconds"],
                          "error": outcome["error"],
                          "accepted": [{"place": row["place"], "url": row["url"]}
                                       for row in outcome["accepted"]],
                          "reviewed_titles": [{"title": item["title"],
                                               "accepted": item["accepted"]}
                                              for item in diagnostics],
                          "total_search_requests": ledger["search_requests"]},
                         ensure_ascii=False))
        if outcome["error"] in ("authentication", "limited"):
            break


if __name__ == "__main__":
    main()
