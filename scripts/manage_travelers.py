"""Create, rotate, or deactivate manually authorized cloud traveler accounts."""

import argparse
import json
import os
from pathlib import Path
import re
from secrets import token_urlsafe
import sys
from uuid import uuid4

from sqlalchemy import select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from travel_assistant.db import engine
from travel_assistant.db.travel_models import travelers
from travel_assistant.core.password_hash import get_hashed_password
from travel_assistant.core.config import settings
from scripts.container_target import assert_container_database_url


USERNAME = re.compile(r"[a-z][a-z0-9_-]{2,39}\Z")
SEEDS = ("demo1", "demo2")


def create(connection, username: str, display_name: str):
    if not USERNAME.fullmatch(username) or username in SEEDS:
        raise ValueError("Choose a unique lowercase username of 3 to 40 characters")
    if not 1 <= len(display_name.strip()) <= 80:
        raise ValueError("Display name must be 1 to 80 characters")
    if connection.execute(select(travelers.c.id).where(
            travelers.c.username == username)).first():
        raise ValueError("Account already exists")
    password = token_urlsafe(32)
    connection.execute(travelers.insert().values(
        id=str(uuid4()), username=username,
        password_hash=get_hashed_password(password),
        display_name=display_name.strip(), active=1,
    ))
    return {"username": username, "password": password}


def rotate(connection, username: str):
    password = token_urlsafe(32)
    result = connection.execute(update(travelers).where(
        travelers.c.username == username, travelers.c.active == 1,
    ).values(password_hash=get_hashed_password(password)))
    if result.rowcount != 1:
        raise ValueError("Active account not found")
    return {"username": username, "password": password}


def disable(connection, username: str):
    result = connection.execute(update(travelers).where(
        travelers.c.username == username, travelers.c.active == 1,
    ).values(active=0))
    if result.rowcount != 1:
        raise ValueError("Active account not found")


def disable_seeds(connection):
    connection.execute(update(travelers).where(
        travelers.c.username.in_(SEEDS)).values(active=0))


def main():
    assert_container_database_url(engine.url)
    if not settings.CLOUD_MODE:
        raise RuntimeError("Account administration is only available in cloud containers")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("create")
    add.add_argument("username")
    add.add_argument("display_name")
    add.add_argument("--output", type=Path, required=True,
                     help="New ignored file for one-time credentials")
    rotate_cmd = commands.add_parser("rotate")
    rotate_cmd.add_argument("username")
    rotate_cmd.add_argument("--output", type=Path, required=True)
    disable_cmd = commands.add_parser("disable")
    disable_cmd.add_argument("username")
    commands.add_parser("disable-seeds")
    args = parser.parse_args()
    if args.command in ("create", "rotate"):
        private_dir = Path(os.getenv("TRAVEL_CREDENTIAL_DIR", "/run/travel-credentials")).resolve()
        if args.output.parent.resolve() != private_dir:
            parser.error("Write credentials only to the mounted private credential directory")
    if args.command in ("create", "rotate") and args.output.exists():
        parser.error("Credential output exists; refusing to overwrite")
    with engine.begin() as connection:
        if args.command == "create":
            credentials = create(connection, args.username, args.display_name)
        elif args.command == "rotate":
            credentials = rotate(connection, args.username)
        elif args.command == "disable":
            disable(connection, args.username)
            credentials = None
        else:
            disable_seeds(connection)
            credentials = None
        if credentials:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(credentials, output, ensure_ascii=False)
                output.write("\n")
    print("Account operation completed; no password is printed.")


if __name__ == "__main__":
    main()
