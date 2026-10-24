"""Prove that a token signed for the prior demo batch cannot enter the new one."""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from dotenv import dotenv_values
from fastapi.testclient import TestClient
from jose import jwt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main import Server
from scripts.batch_environment import checked_path
from travel_assistant.core.config import settings
from travel_assistant.db import engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-port", type=int, required=True)
    parser.add_argument("--batch-env", required=True)
    args = parser.parse_args()
    if (settings.DATABASE_URL or engine.url.port != args.expected_port or
            engine.url.host != "127.0.0.1"):
        parser.error("New batch database target differs from expected port")
    old_values = dotenv_values(ROOT / ".env")
    previous_secret = old_values.get("JWT_SECRET_KEY")
    previous_password = old_values.get("DEMO_TRAVELER_1_PASSWORD")
    if not previous_secret or not settings.JWT_SECRET_KEY:
        parser.error("Both old and new signing secrets are required")
    if previous_secret == settings.JWT_SECRET_KEY:
        parser.error("Old and new batches share a signing secret")
    batch_password = dotenv_values(checked_path(args.batch_env)).get(
        "DEMO_TRAVELER_1_PASSWORD")
    if not batch_password:
        parser.error("New batch demo password is missing")
    if not previous_password or previous_password == batch_password:
        parser.error("Old and new batches lack separate demo passwords")
    old_token = jwt.encode({"sub": "demo_traveler_1", "type": "traveler_access",
                            "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
                           previous_secret, algorithm=settings.ALGORITHM)
    server = Server()
    server.init_app()
    with TestClient(server.app) as client:
        denied = client.get("/api/v1/me", headers={"Authorization": f"Bearer {old_token}"})
        old_login = client.post("/api/v1/login", json={
            "username": "demo1", "password": previous_password})
        accepted = client.post("/api/v1/login", json={
            "username": "demo1", "password": batch_password})
        if accepted.status_code != 200:
            raise RuntimeError(f"New batch login failed: {accepted.status_code}")
        own = client.get("/api/v1/me", headers={
            "Authorization": f"Bearer {accepted.json()['access_token']}"})
    if denied.status_code != 401 or old_login.status_code != 401 or own.status_code != 200:
        raise RuntimeError("Batch token isolation failed")
    print("Old signing token and old password: 401; new login and token: 200")


if __name__ == "__main__":
    main()
