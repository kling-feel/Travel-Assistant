"""Read-only check after restarting the isolated 8020 app and database."""

import httpx
from verify_cloud_acceptance import CREDENTIALS, login, require


def main():
    if not (CREDENTIALS / "reviewer1.json").is_file():
        raise RuntimeError("Test account file is missing")
    with httpx.Client(base_url="http://127.0.0.1:8020", timeout=10) as client:
        if client.get("/ready").status_code != 200:
            raise RuntimeError("Isolated application is not ready")
        one = login(client, "reviewer1")
        two = login(client, "reviewer2")
        sessions = require(client.get("/api/v1/sessions", headers=one), 200)
        first = require(client.get("/api/v1/me/itinerary", headers=one), 200)
        second = require(client.get("/api/v1/me/itinerary", headers=two), 200)
        if len(sessions) < 1 or len(first["hotel_orders"]) != 1 or second["hotel_orders"]:
            raise RuntimeError("Session or confirmed order did not survive restart")
        require(client.get("/api/v1/me/provider-usage", headers=one), 200)
    print("Isolated restart passed: authorized login, session, owner-only confirmed order, usage read")


if __name__ == "__main__":
    main()
