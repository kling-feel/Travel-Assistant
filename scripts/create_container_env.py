"""Create one ignored credential file for an isolated local Compose demo."""

import argparse
from datetime import date
from pathlib import Path
from secrets import token_urlsafe


ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "data" / "generated"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--app-port", type=int, default=8018)
    args = parser.parse_args()
    if not 1024 <= args.app_port <= 65535 or args.app_port == 8000:
        parser.error("Choose an isolated application port")
    if args.as_of > date.today():
        parser.error("The fictional batch date cannot be in the future")
    GENERATED.mkdir(parents=True, exist_ok=True)
    target = GENERATED / "p34a-container.env"
    values = {
        "POSTGRES_PASSWORD": token_urlsafe(32),
        "JWT_SECRET_KEY": token_urlsafe(48),
        "DEMO_TRAVELER_1_PASSWORD": token_urlsafe(24),
        "DEMO_TRAVELER_2_PASSWORD": token_urlsafe(24),
        "DEMO_BATCH_DATE": args.as_of.isoformat(),
        "DEMO_APP_PORT": str(args.app_port),
        "ORIGINS": f"http://127.0.0.1:{args.app_port},http://localhost:{args.app_port}",
    }
    with target.open("x", encoding="utf-8") as output:
        output.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    print("Created ignored local container credentials")


if __name__ == "__main__":
    main()
