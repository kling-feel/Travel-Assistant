"""Start only the independently initialized Compose demo batch."""

import json
import os
from pathlib import Path
import sys

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.container_target import assert_container_database_url
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
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("as_of") != os.getenv("DEMO_BATCH_DATE"):
        raise RuntimeError("Container batch date differs from the manifest")
    with engine.connect() as connection:
        report = summarize(connection)
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
