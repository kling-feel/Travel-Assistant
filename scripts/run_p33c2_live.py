"""Start the local new-batch demo only after checking its actual database target."""

import argparse
from pathlib import Path
import sys
from unittest.mock import patch

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import run_p31h3_metered as meter
from scripts.inspect_demo_batch import summarize
from main import Server
from travel_assistant.core.config import settings
from travel_assistant.db import engine
from travel_assistant.services.recommendations import CleverSeeRecommendationProvider
from travel_assistant.services.weather_search import search_weather


EXPECTED_FIXTURE = "c7a69be6ffc5fcfd0502041e1c5c2844b24e904d0b138dad148bbc8d12bf790f"
EXPECTED_SUPPLY = "0a2932c104826aa96a509dea648618ebca350e48fc3a7ed4166c23f1aa4812cc"


def checked_batch():
    url = engine.url
    if (settings.HOST != "127.0.0.1" or settings.PORT != 8000 or
            settings.POSTGRES_PORT != 5436 or settings.DATABASE_URL or
            url.drivername != "postgresql+psycopg" or
            url.host != "127.0.0.1" or url.port != 5436 or
            url.database != "travel_demo" or url.username != "travel_app" or
            settings.AI_PROVIDER != "deepseek" or not settings.AI_API_KEY or
            not settings.ALIYUN_IQS_ENABLED or not settings.ALIYUN_IQS_API_KEY):
        raise RuntimeError("Live demo requires the isolated 5436 database and local port 8000")
    with engine.connect() as connection:
        report = summarize(connection)
    if (report["as_of"] != "2026-10-24" or
            report["fixture_sha256"] != EXPECTED_FIXTURE or
            report["supply_digest"] != EXPECTED_SUPPLY or
            len(report["migration_versions"]) != 13 or
            report["non_demo_accounts"] or report["counts"]["travelers"] != 2):
        raise RuntimeError("New batch identity or supply no longer matches acceptance")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true",
                        help="Count at most five model and three search calls during cutover smoke test")
    args = parser.parse_args()
    report = checked_batch()
    print(f"Verified local demo target: 127.0.0.1:8000 -> 127.0.0.1:5436; "
          f"batch {report['as_of']}", flush=True)
    server = Server()
    server.init_app()
    if not args.smoke:
        uvicorn.run(server.app, host=settings.HOST, port=settings.PORT)
        return
    meter.LEDGER = ROOT / "data/generated/p33c2-smoke-ledger.json"
    meter.MODEL_CAP = 5
    meter.SEARCH_CAP = 3
    meter.MODEL_RMB_CAP = 2.0
    meter.RETRYABLE_FAILURES = frozenset({"APIConnectionError"})
    if any(row.get("status") == "started" for row in meter._rows()):
        raise RuntimeError("Unsettled paid smoke attempt requires review")
    model = meter.CountedModel()
    live_search = CleverSeeRecommendationProvider.search
    with (patch("travel_assistant.api.travel.get_chat_model", return_value=model),
          patch.object(CleverSeeRecommendationProvider, "search",
                       meter._count_search(live_search, "place")),
          patch("travel_assistant.services.agent_reads.search_weather",
                side_effect=meter._count_search(search_weather, "weather"))):
        uvicorn.run(server.app, host=settings.HOST, port=settings.PORT)


if __name__ == "__main__":
    main()
