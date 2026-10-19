"""Bounded GlobalBasic comparison against the reviewed place catalog."""

import argparse
import json
from pathlib import Path
from time import monotonic

from config import settings
from services.recommendation_geography import country_for_fixture_city
from services.recommendations import (CleverSeeRecommendationProvider,
                                      RecommendationSearchError)
from services.reviewed_places import places_for_city


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "data/generated/p27e-search-ledger.json"
LIMIT = 60
CASES = (
    ("Beijing", "history"), ("Shanghai", "history"),
    ("Kunming", "nature"), ("Hong Kong", "culture"),
    ("Tokyo", "history"), ("Seoul", "history"),
    ("Bangkok", "history"), ("Singapore", "nature"),
    ("Siem Reap", "history"), ("Manila", "history"),
    ("Phuket", "history"), ("Yangon", "history"),
    ("Basel", "architecture"), ("Paris", "culture"),
    ("Rome", "history"), ("London", "history"),
    ("Barcelona", "architecture"), ("Amsterdam", "culture"),
    ("Lisbon", "family"), ("Munich", "family"),
    ("Stockholm", "history"), ("Nagoya", "history"),
    ("New York", "nature"), ("Newark", "culture"),
    ("Washington, D.C.", "history"), ("Cancún", "history"),
    ("Bogotá", "culture"), ("São Paulo", "culture"),
    ("Rio de Janeiro", "culture"), ("Los Angeles", "culture"),
    ("Arusha", "culture"), ("Nairobi", "history"),
    ("Dakar", "history"), ("Johannesburg", "history"),
    ("Doha", "culture"), ("Dubai", "architecture"),
    ("Victoria", "history"), ("Adelaide", "nature"),
    ("Auckland", "history"), ("Sydney", "nature"),
    ("Papeete", "nature"), ("Melbourne", "nature"),
)
NEXT_REGION_SAMPLE = (
    "Rome", "Newark", "Victoria", "Papeete", "Arusha", "Doha",
    "Sydney", "Bogotá", "Barcelona", "Washington, D.C.",
)


def save(data: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    temporary = LEDGER.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    temporary.replace(LEDGER)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-new", type=int, default=6)
    args = parser.parse_args()
    if not 1 <= args.max_new <= 12:
        parser.error("--max-new must be 1..12")
    if not settings.ALIYUN_IQS_API_KEY.strip():
        parser.error("GlobalBasic key is missing")
    if settings.ALIYUN_IQS_ENABLED:
        parser.error("the regular live-search switch must remain off")
    ledger = (json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists()
              else {"attempts": 0, "limit": LIMIT, "rows": []})
    completed = {(row["city"], row["preference"]) for row in ledger["rows"]}
    cases = [case for case in CASES if case not in completed]
    cases.sort(key=lambda case: (NEXT_REGION_SAMPLE.index(case[0])
                                 if case[0] in NEXT_REGION_SAMPLE else 100))
    cases = cases[:args.max_new]
    for city, preference in cases:
        if ledger["attempts"] >= LIMIT:
            raise RuntimeError("P2.7e search cap reached")
        country = country_for_fixture_city(city)
        if country is None:
            raise RuntimeError(f"unverified city: {city}")
        catalog = places_for_city(city, country["code"], preference)
        names = {name.casefold() for place in catalog
                 for name in (place["name"], *place["aliases"])}
        ledger["attempts"] += 1  # Reserve before the paid request.
        save(ledger)
        diagnostics = []
        provider = CleverSeeRecommendationProvider(
            settings.ALIYUN_IQS_API_KEY.strip(), diagnostics=diagnostics)
        started = monotonic()
        try:
            accepted = provider.search(city, preference, country)
            error = None
        except RecommendationSearchError as exc:
            accepted, error = [], exc.kind
        matches = [row["place"] for row in accepted
                   if row["place"].casefold() in names]
        result = {"city": city, "country_code": country["code"],
                  "preference": preference, "catalog_count": len(catalog),
                  "accepted": accepted, "diagnostics": diagnostics,
                  "matches": matches, "error": error,
                  "elapsed_seconds": round(monotonic() - started, 2)}
        ledger["rows"].append(result)
        save(ledger)
        print(json.dumps({"city": city, "preference": preference,
                          "catalog_count": len(catalog),
                          "candidate_count": len(diagnostics),
                          "accepted_count": len(accepted),
                          "matched_count": len(matches), "error": error,
                          "attempts": ledger["attempts"]},
                         ensure_ascii=False), flush=True)
        if error in ("authentication", "limited", "upstream", "invalid"):
            break


if __name__ == "__main__":
    main()
