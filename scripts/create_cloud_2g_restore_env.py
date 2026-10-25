"""Create separate ignored credentials for the 2 GiB restore trial."""

from pathlib import Path
from secrets import token_urlsafe


ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "data/generated"
SOURCE = GENERATED / "p34b-2g.env"
TARGET = GENERATED / "p34b-2g-restore.env"
OVERRIDE = GENERATED / "p34b-2g-restore.override.yaml"


def main():
    if TARGET.exists() or OVERRIDE.exists():
        raise RuntimeError("Restore trial configuration already exists")
    values = dict(line.split("=", 1) for line in SOURCE.read_text(encoding="utf-8").splitlines()
                  if "=" in line)
    if values.get("CLOUD_ACCEPTANCE_MODE") != "true":
        raise RuntimeError("Source is not an isolated acceptance configuration")
    values.update({
        "TRAVEL_CLOUD_ENV_FILE": "./data/generated/p34b-2g-restore.env",
        "POSTGRES_PASSWORD": token_urlsafe(32),
        "JWT_SECRET_KEY": token_urlsafe(48),
        "DEMO_TRAVELER_1_PASSWORD": token_urlsafe(24),
        "DEMO_TRAVELER_2_PASSWORD": token_urlsafe(24),
    })
    with TARGET.open("x", encoding="utf-8") as output:
        output.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    with OVERRIDE.open("x", encoding="utf-8") as output:
        output.write("""# Fresh private restore volume; do not start its application.
services:
  db:
    mem_limit: 640m
    cpus: 0.75
  init:
    mem_limit: 768m
    cpus: 1.0
""")
    print("Created separate 2 GiB restore-trial configuration")


if __name__ == "__main__":
    main()
