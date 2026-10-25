"""Print a credential-free summary of the Compose database for recovery checks."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.container_target import assert_container_database_url
from scripts.inspect_demo_batch import summarize
from travel_assistant.db import engine


def main():
    assert_container_database_url(engine.url)
    with engine.connect() as connection:
        report = summarize(connection)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
