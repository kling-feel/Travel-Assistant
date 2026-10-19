"""Bounded live source sweep; raw search observations stay outside Git."""

import argparse
import json
import sqlite3
import time
from pathlib import Path

from config import settings
from services.recommendation_geography import country_for_fixture_city
from services.recommendations import (CleverSeeRecommendationProvider,
                                      RecommendationSearchError)


ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data/generated/p27c-budget-ledger.json"
FIXTURE = ROOT / "data/fixtures/network_source.sqlite"
LIMIT = 250
PILOT = ("Basel", "Tokyo", "Paris", "Rome", "London", "New York",
         "Beijing", "Dubai", "Bangkok", "São Paulo", "Victoria")
RISK = (
    ("Rome", "culture"), ("Paris", "culture"), ("London", "history"),
    ("New York", "culture"), ("Basel", "architecture"),
    ("Tokyo", "culture"), ("Beijing", "culture"),
    ("Bangkok", "food"), ("Dubai", "architecture"),
    ("Victoria", "nature"), ("Washington, D.C.", "culture"),
    ("Ko Samui", "nature"), ("Cancún", "nature"),
    ("São Paulo", "culture"), ("Hong Kong", "culture"),
    ("Taipei", "culture"), ("Papeete", "nature"),
    ("Istanbul", "architecture"), ("Sydney", "nature"),
    ("Amsterdam", "culture"),
)


def reachable_cities() -> list[str]:
    with sqlite3.connect(FIXTURE) as db:
        rows = db.execute(
            "SELECT DISTINCT a.city FROM flights f "
            "JOIN airports_data a ON a.airport_code=f.arrival_airport "
            "WHERE f.status='Scheduled' ORDER BY a.city"
        ).fetchall()
    cities = [row[0] for row in rows]
    if len(cities) != 95:
        raise RuntimeError("expected 95 reachable fixture cities")
    return cities


def load() -> dict:
    if not LEDGER.exists():
        return {"search_requests": 0, "deepseek_requests": 0,
                "samples": [], "note": "P2.7c independent 250-search/30-model cap"}
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def save(ledger: dict) -> None:
    temporary = LEDGER.with_suffix(".tmp")
    temporary.write_text(json.dumps(ledger, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    temporary.replace(LEDGER)


def reserve_search(ledger: dict) -> None:
    if ledger["search_requests"] >= LIMIT:
        raise RuntimeError("P2.7c search cap reached")
    ledger["search_requests"] += 1
    save(ledger)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("pilot", "sweep", "risk", "retest"),
                        required=True)
    parser.add_argument("--max-new", type=int, default=10)
    parser.add_argument("--city")
    parser.add_argument("--preference", choices=("history", "culture", "nature",
                                                  "architecture", "family", "food"),
                        default="history")
    args = parser.parse_args()
    if not 1 <= args.max_new <= 25:
        parser.error("--max-new must be 1..25 for quality checkpoints")
    if not settings.ALIYUN_IQS_API_KEY.strip():
        parser.error("local search key is not configured")
    cities = reachable_cities()
    if args.stage == "pilot":
        cases = [(city, "history") for city in PILOT if city in cities]
    elif args.stage == "sweep":
        cases = [(city, "history") for city in cities]
    elif args.stage == "risk":
        cases = [(city, preference) for city, preference in RISK if city in cities]
    else:
        if args.city not in cities:
            parser.error("retest city is not reachable")
        cases = [(args.city, args.preference)]
    ledger = load()
    completed = {(row["city"], row["preference"])
                 for row in ledger["samples"]}
    if args.stage != "retest":
        cases = [case for case in cases if case not in completed]
    for city, preference in cases[:args.max_new]:
        country = country_for_fixture_city(city)
        if country is None:
            ledger["samples"].append({"stage": args.stage, "city": city,
                                      "preference": preference,
                                      "error": "ambiguous_geography", "accepted": []})
            save(ledger)
            continue
        reserve_search(ledger)
        diagnostics = []
        provider = CleverSeeRecommendationProvider(
            settings.ALIYUN_IQS_API_KEY.strip(), diagnostics=diagnostics)
        started = time.monotonic()
        try:
            accepted = provider.search(city, preference, country)
            error = None
        except RecommendationSearchError as exc:
            accepted, error = [], exc.kind
        row = {"stage": args.stage, "city": city,
               "country": country["name"], "preference": preference,
               "elapsed_seconds": round(time.monotonic() - started, 2),
               "error": error, "accepted": accepted,
               "diagnostics": diagnostics}
        ledger["samples"].append(row)
        save(ledger)
        print(json.dumps({"city": city, "country": country["name"],
                          "preference": preference, "error": error,
                          "accepted_places": [item["place"] for item in accepted],
                          "accepted_count": len(accepted),
                          "candidate_count": len(diagnostics),
                          "search_requests": ledger["search_requests"]},
                         ensure_ascii=False))
        if error in ("authentication", "limited", "upstream", "invalid"):
            break


if __name__ == "__main__":
    main()
