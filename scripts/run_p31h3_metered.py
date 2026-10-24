"""Run the isolated traveler page with bounded, locally recorded paid calls."""

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from travel_assistant.core.config import settings
from main import Server
from services.answer_composer import compose_verified_answer
from services.model_config import get_chat_model
from services.recommendations import CleverSeeRecommendationProvider
from services.weather_search import search_weather


LEDGER = ROOT / "data/generated/p31h3-paid-ledger.json"
MODEL_CAP = 20
SEARCH_CAP = 10
MODEL_RMB_CAP = 8.0
RETRYABLE_FAILURES = frozenset()
UNKNOWN_RMB_RESERVE = 0.2
_guard = threading.Lock()


def _rows():
    return json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else []


def _save(rows):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _reserve(kind, path=None):
    with _guard:
        rows = _rows()
        model_rows = [row for row in rows if row["kind"] == "model"]
        if kind == "model":
            estimate = sum(row.get("upper_rmb", UNKNOWN_RMB_RESERVE
                                   if row.get("status") in RETRYABLE_FAILURES else 0)
                           for row in model_rows)
            if (len(model_rows) >= MODEL_CAP or estimate + UNKNOWN_RMB_RESERVE > MODEL_RMB_CAP or
                    any(row.get("status") not in ("ok", "sandbox_denied", *RETRYABLE_FAILURES)
                        for row in model_rows)):
                raise RuntimeError("P3.1h3 model attempt, usage, or cost limit reached")
        elif sum(row["kind"] == "search" for row in rows) >= SEARCH_CAP:
            raise RuntimeError("P3.1h3 search attempt limit reached")
        row = {"kind": kind, "path": path, "at": datetime.now(timezone.utc).isoformat(),
               "status": "started"}
        rows.append(row)
        _save(rows)
        return len(rows) - 1


def _finish(index, **fields):
    with _guard:
        rows = _rows()
        rows[index].update(fields)
        _save(rows)


class CountedModel:
    def __init__(self):
        self.backend = get_chat_model().backend

    def invoke(self, messages):
        index = _reserve("model")
        start = perf_counter()
        try:
            reply = self.backend.invoke(messages)
            usage = (getattr(reply, "response_metadata", {}) or {}).get("token_usage", {}) or {}
            fallback = getattr(reply, "usage_metadata", {}) or {}
            incoming = usage.get("prompt_tokens", fallback.get("input_tokens"))
            outgoing = usage.get("completion_tokens", fallback.get("output_tokens"))
            if not isinstance(incoming, int) or not isinstance(outgoing, int):
                _finish(index, status="usage_missing", seconds=round(perf_counter()-start, 3))
                raise RuntimeError("Model usage missing; stop paid requests")
            _finish(index, status="ok", input_tokens=incoming, output_tokens=outgoing,
                    upper_rmb=round((incoming * 2 + outgoing * 8) / 1_000_000, 6),
                    seconds=round(perf_counter()-start, 3))
            return reply
        except Exception as error:
            with _guard:
                current = _rows()[index]["status"]
            if current == "started":
                name = type(error).__name__
                _finish(index, status=name,
                        **({"upper_rmb": UNKNOWN_RMB_RESERVE}
                           if name in RETRYABLE_FAILURES else {}),
                        seconds=round(perf_counter()-start, 3))
            raise

    def compose(self, intent, user_text, answer, references):
        return compose_verified_answer(self, intent, user_text, answer, references)


def _count_search(method, path):
    def run(*args, **kwargs):
        index = _reserve("search", path)
        start = perf_counter()
        try:
            result = method(*args, **kwargs)
            _finish(index, status="ok", seconds=round(perf_counter()-start, 3))
            return result
        except Exception as error:
            _finish(index, status=type(error).__name__,
                    seconds=round(perf_counter()-start, 3))
            raise
    return run


def main():
    assert settings.POSTGRES_PORT == 5435 and not settings.DATABASE_URL
    assert settings.PORT == 8014 and settings.HOST == "127.0.0.1"
    assert settings.AI_PROVIDER == "deepseek" and settings.AI_API_KEY
    assert settings.ALIYUN_IQS_ENABLED and settings.ALIYUN_IQS_API_KEY
    model = CountedModel()
    provider_search = CleverSeeRecommendationProvider.search
    server = Server()
    server.init_app()
    with (patch("api.travel.get_chat_model", return_value=model),
          patch.object(CleverSeeRecommendationProvider, "search",
                       _count_search(provider_search, "place")),
          patch("services.agent_reads.search_weather",
                side_effect=_count_search(search_weather, "weather"))):
        uvicorn.run(server.app, host=settings.HOST, port=settings.PORT)


if __name__ == "__main__":
    main()
