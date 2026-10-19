"""Print a bounded source-review queue from the ignored live-test ledger."""

import argparse
import json

from scripts.evaluate_p27c_sources import LEDGER, reachable_cities
from services.recommendation_geography import country_for_fixture_city
from services.recommendations import _search_record, _source_rank


def queue() -> list[dict]:
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    samples = {}
    for row in reversed(ledger["samples"]):
        if row.get("error") is None:
            samples.setdefault(row["city"], []).append(row)
    rows = []
    for city in reachable_cities():
        city_samples = samples.get(city, [])
        if not city_samples:
            continue
        found = set()
        for sample in city_samples:
            for candidate in sample["accepted"]:
                item = {"title": candidate["title"], "snippet": candidate["excerpt"],
                        "link": candidate["url"]}
                record = _search_record(item, city, sample["preference"],
                                        candidate["retrieved_at"],
                                        country_for_fixture_city(city))
                if record and record["url"] not in found:
                    found.add(record["url"])
                    rows.append({"city": city, "place": record["place"],
                                 "url": record["url"],
                                 "rank": _source_rank(record["url"])})
    return sorted(rows, key=lambda row: (row["city"].casefold(), row["rank"]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--first-per-city", action="store_true")
    args = parser.parse_args()
    rows = queue()
    if args.first_per_city:
        seen = set()
        rows = [row for row in rows if not (row["city"] in seen or seen.add(row["city"]))]
    print(json.dumps({"cities": len({row["city"] for row in rows}),
                      "candidates": len(rows),
                      "rows": rows[args.offset:args.offset + args.limit]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
