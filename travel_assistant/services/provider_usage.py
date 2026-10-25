"""Record provider request metadata without storing prompts, responses, or keys."""

from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import text

from travel_assistant.core.config import settings
from travel_assistant.db import engine


logger = logging.getLogger(__name__)
current_traveler = ContextVar("provider_usage_traveler", default=None)
OUTCOMES = {"success", "authentication", "balance", "limited", "timeout", "upstream"}


class ProviderBalanceError(RuntimeError):
    """Only a safe failure category is exposed to callers."""


def classify_error(error):
    code = getattr(error, "status_code", None)
    if code in (401, 403):
        return "authentication"
    if code == 402:
        return "balance"
    if code == 429:
        return "limited"
    if isinstance(error, TimeoutError) or "Timeout" in type(error).__name__:
        return "timeout"
    return "upstream"


def record(provider: str, outcome: str, *, input_tokens=None, output_tokens=None):
    if not settings.CLOUD_MODE:
        return
    if provider not in ("model", "search") or outcome not in OUTCOMES:
        raise ValueError("Invalid provider usage event")
    try:
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO provider_usage_events
                    (traveler_id, provider, outcome, input_tokens, output_tokens)
                VALUES (:traveler_id, :provider, :outcome, :input_tokens, :output_tokens)
            """), {"traveler_id": current_traveler.get(), "provider": provider,
                   "outcome": outcome, "input_tokens": input_tokens,
                   "output_tokens": output_tokens})
    except Exception as error:
        logger.warning("provider_usage_record_failed reason=%s", type(error).__name__)


def summary(connection, traveler_id=None, days=7):
    if not 1 <= days <= 31:
        raise ValueError("days must be between 1 and 31")
    since = datetime.now(timezone.utc) - timedelta(days=days)
    owner_filter = "AND traveler_id = :traveler_id" if traveler_id is not None else ""
    rows = connection.execute(text(f"""
        SELECT provider, outcome, count(*) AS requests,
               coalesce(sum(input_tokens), 0) AS input_tokens,
               coalesce(sum(output_tokens), 0) AS output_tokens
        FROM provider_usage_events
        WHERE created_at >= :since
          {owner_filter}
        GROUP BY provider, outcome
        ORDER BY provider, outcome
    """), {"since": since, "traveler_id": traveler_id}).mappings().all()
    return {"days": days, "counts": [dict(row) for row in rows],
            "billing_note": "这里记录调用次数和模型返回的用量；实际费用以服务商账单为准。"}
