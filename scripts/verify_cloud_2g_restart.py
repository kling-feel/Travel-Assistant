"""Read-only persistence check after an isolated 2 GiB app or DB restart."""

import json
from pathlib import Path
import time

import httpx


ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = ROOT / "data/generated/cloud-accounts/cap2g1.json"


def main():
    account = json.loads(ACCOUNT.read_text(encoding="utf-8"))
    with httpx.Client(base_url="http://127.0.0.1:8021", timeout=5) as client:
        for _ in range(20):
            try:
                if client.get("/ready").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("App did not become ready after restart")
        login = client.post("/api/v1/login", json=account)
        if login.status_code != 200:
            raise RuntimeError(f"Authorized login failed: {login.status_code}")
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        sessions = client.get("/api/v1/sessions", headers=headers)
        itinerary = client.get("/api/v1/me/itinerary", headers=headers)
        if sessions.status_code != 200 or not sessions.json():
            raise RuntimeError("Previous session missing after restart")
        if itinerary.status_code != 200 or len(itinerary.json()["hotel_orders"]) != 1:
            raise RuntimeError("Confirmed hotel order missing after restart")
    print("Isolated app ready; authorized session and confirmed order persisted")


if __name__ == "__main__":
    main()
