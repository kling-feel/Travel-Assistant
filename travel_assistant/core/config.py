"""Small environment-based configuration for local and future cloud runs."""

import os

from dotenv import load_dotenv

from travel_assistant.paths import PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")


class Settings:
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    HOST = os.getenv("HOST", "127.0.0.1")
    PORT = int(os.getenv("PORT", "8000"))
    ORIGINS = [
        value.strip()
        for value in os.getenv("ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",")
    ]
    WHITE_LIST = [
        r"^/api/(login|register|auth)/?$",
        r"^/(docs|redoc|openapi.json)(/|$)",
        r"^/static/",
        r"^/health$",
        r"^/ready$",
    ]
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
    ALGORITHM = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
    POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")
    POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5433"))
    DATABASE_URL = os.getenv("DATABASE_URL", "")
    AI_PROVIDER = os.getenv("AI_PROVIDER", "deepseek")
    AI_MODEL = os.getenv("AI_MODEL", "")
    AI_API_KEY = os.getenv("AI_API_KEY", "")
    AI_BASE_URL = os.getenv("AI_BASE_URL", "")
    ALIYUN_IQS_API_KEY = os.getenv("ALIYUN_IQS_API_KEY", "")
    ALIYUN_IQS_ENABLED = os.getenv("ALIYUN_IQS_ENABLED", "true").lower() == "true"


settings = Settings()
