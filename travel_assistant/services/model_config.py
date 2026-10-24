"""Model construction at the conversation boundary; secrets stay in .env."""

from langchain_openai import ChatOpenAI

from travel_assistant.core.config import settings
from travel_assistant.services.answer_composer import ComposingModel


class ModelUnavailable(RuntimeError):
    pass


def get_chat_model():
    provider = settings.AI_PROVIDER.lower().strip()
    if provider not in ("deepseek", "gpt", "local"):
        raise ModelUnavailable("AI_PROVIDER must be deepseek, gpt or local")
    if not settings.AI_MODEL or not settings.AI_BASE_URL:
        raise ModelUnavailable("AI_MODEL and AI_BASE_URL are not configured")
    if provider != "local" and not settings.AI_API_KEY:
        raise ModelUnavailable("AI_API_KEY is not configured")
    return ComposingModel(ChatOpenAI(
        model=settings.AI_MODEL,
        base_url=settings.AI_BASE_URL,
        api_key=settings.AI_API_KEY or "local-no-key",
        temperature=0,
        timeout=25,
        max_retries=0,
        max_tokens=4096,
    ))
