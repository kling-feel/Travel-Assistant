"""Run the isolated 8017 acceptance page with capped, recorded paid calls."""

import sys
from pathlib import Path
from unittest.mock import patch

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import run_p31h3_metered as meter
from main import Server
from travel_assistant.core.config import settings
from travel_assistant.db import engine
from travel_assistant.services.recommendations import CleverSeeRecommendationProvider
from travel_assistant.services.weather_search import search_weather


meter.LEDGER = ROOT / "data/generated/p33c1-paid-ledger.json"
meter.MODEL_CAP = 40
meter.SEARCH_CAP = 30
meter.MODEL_RMB_CAP = 8.0
meter.RETRYABLE_FAILURES = frozenset({"APIConnectionError"})


def main():
    url = engine.url
    if (settings.HOST != "127.0.0.1" or settings.PORT != 8017 or
            settings.POSTGRES_PORT != 5438 or settings.DATABASE_URL or
            url.host != "127.0.0.1" or url.port != 5438 or
            url.database != "travel_demo" or url.username != "travel_app" or
            settings.AI_PROVIDER != "deepseek" or not settings.AI_API_KEY or
            not settings.ALIYUN_IQS_ENABLED or not settings.ALIYUN_IQS_API_KEY):
        raise RuntimeError("Paid acceptance requires isolated 5438 / 8017 settings")
    if any(row.get("status") == "started" for row in meter._rows()):
        raise RuntimeError("Unsettled paid attempt requires review before restart")
    model = meter.CountedModel()
    provider_search = CleverSeeRecommendationProvider.search
    server = Server()
    server.init_app()
    with (patch("travel_assistant.api.travel.get_chat_model", return_value=model),
          patch.object(CleverSeeRecommendationProvider, "search",
                       meter._count_search(provider_search, "place")),
          patch("travel_assistant.services.agent_reads.search_weather",
                side_effect=meter._count_search(search_weather, "weather"))):
        uvicorn.run(server.app, host=settings.HOST, port=settings.PORT)


if __name__ == "__main__":
    main()
