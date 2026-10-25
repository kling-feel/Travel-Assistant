"""Create isolated, unusable upstream credentials for a local cloud-path test."""

from datetime import datetime
from pathlib import Path
from secrets import token_urlsafe
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "data/generated/p34b-acceptance.env"


def main():
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    (TARGET.parent / "cloud-accounts").mkdir(exist_ok=True)
    values = {
        "TRAVEL_CLOUD_ENV_FILE": "./data/generated/p34b-acceptance.env",
        "PUBLIC_HOST": "travel.test",
        "ACME_EMAIL": "test@example.org",
        "CLOUD_BIND_ADDRESS": "127.0.0.1",
        "CLOUD_ACCEPTANCE_MODE": "true",
        "POSTGRES_PASSWORD": token_urlsafe(32),
        "JWT_SECRET_KEY": token_urlsafe(48),
        "DEMO_TRAVELER_1_PASSWORD": token_urlsafe(24),
        "DEMO_TRAVELER_2_PASSWORD": token_urlsafe(24),
        "DEMO_BATCH_DATE": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
        "AI_PROVIDER": "deepseek",
        "AI_MODEL": "deepseek-flash",
        "AI_BASE_URL": "https://api.deepseek.com",
        "AI_API_KEY": "local-test-only-" + token_urlsafe(24),
        "ALIYUN_IQS_ENABLED": "true",
        "ALIYUN_IQS_API_KEY": "local-test-only-" + token_urlsafe(24),
        "PYTHON_BASE_IMAGE": "python:3.11-slim-bookworm",
        "POSTGRES_IMAGE": "postgres:17.11-alpine",
        "CADDY_IMAGE": "caddy:2-alpine",
    }
    with TARGET.open("x", encoding="utf-8") as output:
        output.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    print("Created isolated 8020 acceptance configuration with fake provider keys")


if __name__ == "__main__":
    main()
