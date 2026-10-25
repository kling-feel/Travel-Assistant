"""Create a fresh ignored local-only environment for the 2 GiB capacity trial."""

from datetime import datetime
from pathlib import Path
from secrets import token_urlsafe
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "data/generated"
ENV = GENERATED / "p34b-2g.env"
OVERRIDE = GENERATED / "p34b-2g.override.yaml"
CADDY = GENERATED / "p34b-2g.Caddyfile"


def main():
    GENERATED.mkdir(parents=True, exist_ok=True)
    for path in (ENV, OVERRIDE, CADDY):
        if path.exists():
            raise RuntimeError(f"Capacity test file already exists: {path.name}")
    values = {
        "TRAVEL_CLOUD_ENV_FILE": "./data/generated/p34b-2g.env",
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
        "PYTHON_BASE_IMAGE": "python:3.11-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf",
        "POSTGRES_IMAGE": "postgres:17.11-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24",
        "CADDY_IMAGE": "caddy:2-alpine@sha256:eb4d35b7452d1ab6171fbdbe8d9275b2301dad6dad92115d91e19fb9b24d510f",
    }
    with ENV.open("x", encoding="utf-8") as output:
        output.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    with OVERRIDE.open("x", encoding="utf-8") as output:
        output.write("""# Only for a localhost capacity trial. Never use on a public server.
services:
  db:
    mem_limit: 640m
    cpus: 0.75
  backup:
    mem_limit: 256m
  init:
    mem_limit: 768m
    cpus: 1.0
  app:
    mem_limit: 768m
    cpus: 1.0
    ports:
      - \"127.0.0.1:8021:8000\"
  account-admin:
    mem_limit: 768m
    cpus: 1.0
  proxy:
    mem_limit: 128m
    cpus: 0.1
    ports: !override
      - \"127.0.0.1:8022:443\"
    volumes: !override
      - ./data/generated/p34b-2g.Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
""")
    with CADDY.open("x", encoding="utf-8") as output:
        output.write((ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
                     .replace("{$PUBLIC_HOST} {", "{$PUBLIC_HOST} {\n\ttls internal", 1))
    print("Created isolated 2 GiB capacity configuration with unusable provider keys")


if __name__ == "__main__":
    main()
