"""Print provider request counts from the cloud database without user messages."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from travel_assistant.db import engine
from travel_assistant.services.provider_usage import summary
from travel_assistant.core.config import settings
from scripts.container_target import assert_container_database_url


def main():
    assert_container_database_url(engine.url)
    if not settings.CLOUD_MODE:
        raise RuntimeError("Usage report is only available in cloud containers")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--traveler-id", help="Optional account ID for a single account")
    args = parser.parse_args()
    with engine.connect() as connection:
        report = summary(connection, args.traveler_id, args.days)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
