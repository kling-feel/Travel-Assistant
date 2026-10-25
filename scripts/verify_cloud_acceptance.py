"""Check local 8020 cloud-path authentication and one simulated confirmation."""

import json
from datetime import date, timedelta
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
CREDENTIALS = ROOT / "data/generated/cloud-accounts"
ENV = ROOT / "data/generated/p34b-acceptance.env"


def require(response, status):
    if response.status_code != status:
        raise RuntimeError(f"Expected HTTP {status}; received {response.status_code}")
    return response.json()


def login(client, name):
    account = json.loads((CREDENTIALS / f"{name}.json").read_text(encoding="utf-8"))
    result = require(client.post("/api/v1/login", json=account), 200)
    return {"Authorization": "Bearer " + result["access_token"]}


def main():
    if not ENV.is_file() or not all((CREDENTIALS / f"reviewer{number}.json").is_file()
                                     for number in (1, 2)):
        raise RuntimeError("Isolated configuration or test accounts are missing")
    values = dict(line.split("=", 1) for line in ENV.read_text(encoding="utf-8").splitlines()
                  if "=" in line)
    if values.get("CLOUD_ACCEPTANCE_MODE") != "true" or values.get("PUBLIC_HOST") != "travel.test":
        raise RuntimeError("Not the local acceptance configuration")
    with httpx.Client(base_url="http://127.0.0.1:8020", timeout=10) as client:
        if client.get("/app").status_code != 200:
            raise RuntimeError("Login page unavailable")
        if client.get("/docs").status_code != 404 or client.get("/openapi.json").status_code != 404:
            raise RuntimeError("Public API documentation must be disabled")
        if client.get("/api/v1/me/itinerary").status_code not in (401, 403):
            raise RuntimeError("Anonymous itinerary access was not denied")
        if client.post("/api/v1/login", json={"username": "demo1",
                "password": values["DEMO_TRAVELER_1_PASSWORD"]}).status_code != 401:
            raise RuntimeError("Initial seed account is still active")
        one = login(client, "reviewer1")
        two = login(client, "reviewer2")
        usage = require(client.get("/api/v1/me/provider-usage", headers=one), 200)
        if usage["counts"]:
            raise RuntimeError("Test account already has provider calls")
        session = require(client.post("/api/v1/sessions", headers=one, json={}), 201)["id"]
        if client.get(f"/api/v1/sessions/{session}", headers=two).status_code != 404:
            raise RuntimeError("A second account can read another session")
        state = require(client.get("/api/v1/demo-state", headers=one), 200)
        check_in = date.fromisoformat(state["bookable_from"])
        check_out = check_in + timedelta(days=1)
        rooms = require(client.get("/api/v1/hotels", headers=one, params={
            "city": "Basel", "check_in": str(check_in), "check_out": str(check_out),
        }), 200)
        if not rooms:
            raise RuntimeError("Isolated Basel room supply is empty")
        first = rooms[0]
        card = require(client.post(f"/api/v1/sessions/{session}/candidate-proposals",
                                   headers=one, json={
            "kind": "hotel", "candidate_id": first["product_id"],
            "unit_price": first["nightly_price"], "city": "Basel",
            "check_in": str(check_in), "check_out": str(check_out),
        }), 201)
        if card["status"] != "pending":
            raise RuntimeError("No pending review card was created")
        before = require(client.get("/api/v1/me/itinerary", headers=one), 200)
        if before["hotel_orders"]:
            raise RuntimeError("Order changed before traveler confirmation")
        card_url = f"/api/v1/sessions/{session}/actions/{card['id']}"
        if client.get(card_url, headers=two).status_code != 404:
            raise RuntimeError("A second account can read another review card")
        confirmed = require(client.post(card_url + "/confirm", headers=one), 200)
        repeated = require(client.post(card_url + "/confirm", headers=one), 200)
        if confirmed != repeated:
            raise RuntimeError("Repeated confirmation was not idempotent")
        after = require(client.get("/api/v1/me/itinerary", headers=one), 200)
        other = require(client.get("/api/v1/me/itinerary", headers=two), 200)
        if len(after["hotel_orders"]) != 1 or other["hotel_orders"]:
            raise RuntimeError("Confirmed hotel order is missing or crossed accounts")
        for attempt in range(9):
            response = client.post("/api/v1/login", json={
                "username": "unknown-acceptance-account", "password": "wrong-password",
            })
            if response.status_code != (429 if attempt == 8 else 401):
                raise RuntimeError("Login failure rate limit did not activate")
    print("Local cloud-path acceptance passed: login, isolation, card, confirmation, idempotency, rate limit")


if __name__ == "__main__":
    main()
