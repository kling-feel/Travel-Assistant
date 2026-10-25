"""Initialize exactly one empty Compose database after its empty backup exists."""

import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.container_target import assert_container_database_url
from scripts.inspect_demo_batch import summarize
from scripts.migrate import assert_empty_container_database
from scripts.migrate import MIGRATIONS
from travel_assistant.db import engine


BACKUP = Path("/backups/empty.dump")
MANIFEST = Path("/run/travel/batch.json")
FIXTURE = ROOT / "data/fixtures/network_source.sqlite"


def main():
    assert_container_database_url(engine.url)
    as_of = date.fromisoformat(os.environ["DEMO_BATCH_DATE"])
    if as_of > datetime.now(ZoneInfo("Asia/Shanghai")).date():
        raise RuntimeError("The fictional batch date cannot be in the future")
    if MANIFEST.exists():
        raise RuntimeError("Batch manifest already exists; refusing to initialize again")
    if not BACKUP.is_file() or BACKUP.stat().st_size < 5:
        raise RuntimeError("Verified empty database backup is required")
    with BACKUP.open("rb") as source:
        if source.read(5) != b"PGDMP":
            raise RuntimeError("Empty database backup has an invalid format")
    with engine.connect() as connection:
        assert_empty_container_database(connection)
    subprocess.run([sys.executable, str(ROOT / "scripts/migrate.py"),
                    "--fresh-batch-container"], check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts/import_network_data.py"),
                    "--as-of", as_of.isoformat(), "--fresh-batch-container",
                    "--apply", "--reset-demo", "--backup", str(BACKUP)], check=True)
    with engine.connect() as connection:
        report = summarize(connection)
    if (report["as_of"] != as_of.isoformat() or
            report["fixture_sha256"] != hashlib.sha256(FIXTURE.read_bytes()).hexdigest() or
            len(report["migration_versions"]) != len(MIGRATIONS) or
            report["counts"]["airports"] != 115 or
            report["counts"]["flights"] != 33121 or
            report["counts"]["travelers"] != 2):
        raise RuntimeError("Initialized batch summary does not match the fixture")
    manifest = {"as_of": report["as_of"],
                "fixture_sha256": report["fixture_sha256"],
                "supply_digest": report["supply_digest"],
                "migration_versions": report["migration_versions"],
                "empty_backup_sha256": hashlib.sha256(BACKUP.read_bytes()).hexdigest()}
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    staging = MANIFEST.with_suffix(".tmp")
    with staging.open("x", encoding="utf-8") as output:
        json.dump(manifest, output, sort_keys=True)
    staging.replace(MANIFEST)
    print(f"Initialized isolated fictional batch {as_of.isoformat()}")


if __name__ == "__main__":
    main()
