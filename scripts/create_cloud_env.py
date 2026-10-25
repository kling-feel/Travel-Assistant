"""Create an ignored cloud configuration without copying local API credentials."""

import argparse
from datetime import date, datetime
import os
from pathlib import Path
import re
from secrets import token_urlsafe
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.container_target import PUBLIC_DOMAIN


GENERATED = ROOT / "data" / "generated"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True, help="Your verified public domain, without https://")
    parser.add_argument("--email", required=True, help="Certificate renewal contact")
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--name", default="cloud.env", help="New filename under data/generated")
    args = parser.parse_args()
    host = args.domain.strip().lower()
    if (not PUBLIC_DOMAIN.fullmatch(host)
            or host.endswith((".invalid", ".example", ".test"))):
        parser.error("Provide a real DNS hostname")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", args.email):
        parser.error("Provide a contact email address")
    if args.as_of > datetime.now(ZoneInfo("Asia/Shanghai")).date():
        parser.error("The fictional batch date cannot be in the future")
    if Path(args.name).name != args.name or not args.name.endswith(".env"):
        parser.error("Choose a simple .env filename")
    GENERATED.mkdir(parents=True, exist_ok=True)
    target = GENERATED / args.name
    values = {
        "TRAVEL_CLOUD_ENV_FILE": f"./data/generated/{args.name}",
        "PUBLIC_HOST": host,
        "ACME_EMAIL": args.email,
        "POSTGRES_PASSWORD": token_urlsafe(32),
        "JWT_SECRET_KEY": token_urlsafe(48),
        "DEMO_TRAVELER_1_PASSWORD": token_urlsafe(24),
        "DEMO_TRAVELER_2_PASSWORD": token_urlsafe(24),
        "DEMO_BATCH_DATE": args.as_of.isoformat(),
        "AI_PROVIDER": "deepseek",
        "AI_MODEL": "deepseek-flash",
        "AI_BASE_URL": "https://api.deepseek.com",
        "AI_API_KEY": "",
        "ALIYUN_IQS_ENABLED": "true",
        "ALIYUN_IQS_API_KEY": "",
        "PYTHON_BASE_IMAGE": "python:3.11-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf",
        "POSTGRES_IMAGE": "postgres:17.11-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24",
        "CADDY_IMAGE": "caddy:2-alpine@sha256:eb4d35b7452d1ab6171fbdbe8d9275b2301dad6dad92115d91e19fb9b24d510f",
    }
    with target.open("x", encoding="utf-8") as output:
        output.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    if os.name == "posix":
        target.chmod(0o600)
    print(f"Created ignored cloud configuration at {target.relative_to(ROOT)}")
    print("Add new cloud-only API keys locally and review pinned image digests before deployment.")


if __name__ == "__main__":
    main()
