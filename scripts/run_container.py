"""Start only the independently initialized Compose demo batch."""

import json
import os
from pathlib import Path
import re
import sys

import uvicorn
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.container_target import PUBLIC_DOMAIN, assert_container_database_url
from scripts.inspect_demo_batch import summarize
from main import Server
from travel_assistant.core.config import settings
from travel_assistant.db import engine


MANIFEST = Path("/run/travel/batch.json")


def checked_batch():
    assert_container_database_url(engine.url)
    if (settings.HOST != "0.0.0.0" or settings.PORT != 8000 or
            not settings.JWT_SECRET_KEY or
            settings.JWT_SECRET_KEY.startswith("replace-with") or
            not MANIFEST.is_file()):
        raise RuntimeError("Container runtime configuration is incomplete")
    if settings.CLOUD_MODE:
        if (not PUBLIC_DOMAIN.fullmatch(settings.PUBLIC_HOST)
                or settings.PUBLIC_HOST.startswith("localhost.")
                or settings.ORIGINS != [f"https://{settings.PUBLIC_HOST}"]
                or settings.AI_PROVIDER != "deepseek"
                or not settings.AI_MODEL
                or settings.AI_BASE_URL != "https://api.deepseek.com"
                or len(settings.AI_API_KEY) < 20
                or not settings.ALIYUN_IQS_ENABLED
                or len(settings.ALIYUN_IQS_API_KEY) < 20
                or len(settings.JWT_SECRET_KEY) < 48):
            raise RuntimeError("Cloud runtime configuration is incomplete")
        if settings.CLOUD_ACCEPTANCE_MODE:
            if (not settings.PUBLIC_HOST.endswith(".test")
                    or os.getenv("CLOUD_BIND_ADDRESS") != "127.0.0.1"
                    or not settings.AI_API_KEY.startswith("local-test-only-")
                    or not settings.ALIYUN_IQS_API_KEY.startswith("local-test-only-")):
                raise RuntimeError("Local acceptance configuration is incomplete")
        elif (settings.PUBLIC_HOST.endswith((".test", ".example", ".invalid"))
              or settings.AI_API_KEY.startswith("local-test-only-")
              or settings.ALIYUN_IQS_API_KEY.startswith("local-test-only-")
              or any(not re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", os.getenv(name, ""))
                     for name in ("PYTHON_BASE_IMAGE", "POSTGRES_IMAGE", "CADDY_IMAGE"))):
            raise RuntimeError("Production cloud images and credentials are not pinned")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("as_of") != os.getenv("DEMO_BATCH_DATE"):
        raise RuntimeError("Container batch date differs from the manifest")
    with engine.connect() as connection:
        report = summarize(connection)
        if settings.CLOUD_MODE:
            seeded = connection.execute(text(
                "SELECT count(*) FROM travelers WHERE active = 1 AND username IN ('demo1', 'demo2')"
            )).scalar_one()
            authorized = connection.execute(text(
                "SELECT count(*) FROM travelers WHERE active = 1 AND username NOT IN ('demo1', 'demo2')"
            )).scalar_one()
            if seeded or not authorized:
                raise RuntimeError("Cloud accounts must be provisioned and seed accounts disabled")
    if (report["as_of"] != manifest["as_of"] or
            report["fixture_sha256"] != manifest["fixture_sha256"] or
            report["supply_digest"] != manifest["supply_digest"] or
            report["migration_versions"] != manifest["migration_versions"] or
            report["counts"]["travelers"] < 2):
        raise RuntimeError("Container database differs from the initialized batch")
    return report


def main():
    report = checked_batch()
    print(f"Verified isolated container batch {report['as_of']}", flush=True)
    server = Server()
    server.init_app()
    uvicorn.run(server.app, host=settings.HOST, port=settings.PORT)


if __name__ == "__main__":
    main()
