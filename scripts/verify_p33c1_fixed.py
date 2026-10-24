"""Run existing fixed-model journey checks only against the 5438 acceptance batch."""

import argparse
import os
from pathlib import Path
import sys

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import (verify_p25_all_actions, verify_p25_rental_changes,
                     verify_p26b_dialogue)
from travel_assistant.core.config import settings
from travel_assistant.db import engine
from travel_assistant.db.travel_models import demo_state


BACKUP = ROOT / "data/generated/p33c1-repeat-before-acceptance-20261025.dump"
CHECKS = {
    "actions": verify_p25_all_actions,
    "rental-change": verify_p25_rental_changes,
    "dialogue": verify_p26b_dialogue,
}


def assert_acceptance_target():
    url = engine.url
    if (os.getenv("DATABASE_URL") or url.drivername != "postgresql+psycopg" or
            url.host != "127.0.0.1" or url.port != 5438 or
            url.database != "travel_demo" or url.username != "travel_app" or
            settings.POSTGRES_PORT != 5438 or settings.PORT != 8017):
        raise RuntimeError("Verifier requires the isolated 5438 / 8017 batch")
    if not BACKUP.is_file() or BACKUP.stat().st_size < 100_000:
        raise RuntimeError("Verified acceptance backup is required")
    with BACKUP.open("rb") as source:
        if source.read(5) != b"PGDMP":
            raise RuntimeError("Acceptance backup has the wrong format")
    with engine.connect() as connection:
        row = connection.execute(select(demo_state.c.as_of,
                                        demo_state.c.fixture_sha256)).one()
    if (row.as_of.isoformat() != "2026-10-24" or row.fixture_sha256 !=
            "c7a69be6ffc5fcfd0502041e1c5c2844b24e904d0b138dad148bbc8d12bf790f"):
        raise RuntimeError("Acceptance batch date or fixture differs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", choices=CHECKS)
    parser.add_argument("phase", choices=("prepare", "finish"))
    args = parser.parse_args()
    assert_acceptance_target()
    getattr(CHECKS[args.check], args.phase)()


if __name__ == "__main__":
    main()
