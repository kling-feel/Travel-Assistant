"""Create and use ignored, isolated credentials for a fresh demo batch."""

import argparse
import os
from pathlib import Path
from secrets import token_urlsafe
import subprocess
import sys

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "data" / "generated"
KEYS = {"POSTGRES_PASSWORD", "POSTGRES_PORT", "JWT_SECRET_KEY",
        "DEMO_TRAVELER_1_PASSWORD", "DEMO_TRAVELER_2_PASSWORD",
        "PORT", "ORIGINS"}


def checked_path(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_relative_to(GENERATED.resolve()) or path.suffix != ".env":
        raise ValueError("Batch environment must be an ignored .env file under data/generated")
    return path


def create(path: Path, db_port: int, api_port: int):
    if not all(1024 <= port <= 65535 for port in (db_port, api_port)):
        raise ValueError("Batch ports must be between 1024 and 65535")
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "POSTGRES_PASSWORD": token_urlsafe(32),
        "POSTGRES_PORT": str(db_port),
        "JWT_SECRET_KEY": token_urlsafe(48),
        "DEMO_TRAVELER_1_PASSWORD": token_urlsafe(24),
        "DEMO_TRAVELER_2_PASSWORD": token_urlsafe(24),
        "PORT": str(api_port),
        "ORIGINS": f"http://127.0.0.1:{api_port},http://localhost:{api_port}",
    }
    with path.open("x", encoding="utf-8") as output:
        output.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")


def run(path: Path, command: list[str]):
    if not command:
        raise ValueError("Command is required after --")
    values = dotenv_values(path)
    if set(values) != KEYS or any(not value for value in values.values()):
        raise ValueError("Batch environment is incomplete or has unexpected keys")
    environment = os.environ.copy()
    environment.update(values)
    environment["DATABASE_URL"] = ""
    environment["ALIYUN_IQS_ENABLED"] = "false"
    environment["AI_API_KEY"] = ""
    environment["ALIYUN_IQS_API_KEY"] = ""
    return subprocess.run(command, env=environment, check=False).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    new = actions.add_parser("create")
    new.add_argument("path")
    new.add_argument("--db-port", type=int, required=True)
    new.add_argument("--api-port", type=int, required=True)
    launch = actions.add_parser("run")
    launch.add_argument("path")
    launch.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        path = checked_path(args.path)
        if args.action == "create":
            create(path, args.db_port, args.api_port)
            print("Created ignored batch credentials")
            return
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        raise SystemExit(run(path, command))
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
