"""Exercise the isolated 2 GiB app without calling paid model/search providers."""

import argparse
import concurrent.futures
from datetime import date, timedelta
import json
from pathlib import Path
import socket
import ssl
import statistics
import time

import httpx


ROOT = Path(__file__).resolve().parents[1]
CREDS = ROOT / "data/generated/cloud-accounts"
RESULT = ROOT / "data/generated/p34b-2g-workload.json"


def require(response, status=200):
    if response.status_code != status:
        raise RuntimeError(f"Expected {status}, received {response.status_code}")
    return response.json()


def login(client, name):
    account = json.loads((CREDS / f"{name}.json").read_text(encoding="utf-8"))
    token = require(client.post("/api/v1/login", json=account))["access_token"]
    return {"Authorization": "Bearer " + token}


def request(client, headers, path, params=None):
    start = time.perf_counter()
    require(client.get(path, headers=headers, params=params))
    return round((time.perf_counter() - start) * 1000, 2)


def proxy_status(path):
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", 8022), timeout=10) as connection:
        with context.wrap_socket(connection, server_hostname="travel.test") as secure:
            secure.sendall((f"GET {path} HTTP/1.1\r\nHost: travel.test\r\n"
                            "Connection: close\r\n\r\n").encode("ascii"))
            return int(secure.recv(1024).split(b"\r\n", 1)[0].split()[1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batches", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.batches <= 100:
        parser.error("Choose 1 to 100 batches")
    if RESULT.exists():
        raise RuntimeError("Existing workload result; refusing to overwrite")
    with httpx.Client(base_url="http://127.0.0.1:8021", timeout=20) as client:
        if client.get("/app").status_code != 200:
            raise RuntimeError("Capacity app page unavailable")
        one, two = login(client, "cap2g1"), login(client, "cap2g2")
        state = require(client.get("/api/v1/demo-state", headers=one))
        day = date.fromisoformat(state["bookable_from"])
        flights = {"departure": "HAM", "arrival": "BSL", "travel_date": str(day)}
        hotels = {"city": "Basel", "check_in": str(day),
                  "check_out": str(day + timedelta(days=1))}
        rentals = {"city": "Shanghai", "pickup_date": str(day),
                   "drop_off_date": str(day + timedelta(days=2))}
        for path, params in (("/api/v1/flights", flights),
                             ("/api/v1/hotels", hotels),
                             ("/api/v1/rentals", rentals)):
            if not require(client.get(path, headers=one, params=params)):
                raise RuntimeError(f"Empty capacity-test supply: {path}")
        session = require(client.post("/api/v1/sessions", headers=one, json={}), 201)["id"]
        if client.get(f"/api/v1/sessions/{session}", headers=two).status_code != 404:
            raise RuntimeError("Cross-account read was permitted")
        before = require(client.get("/api/v1/me/itinerary", headers=one))
        if not before["hotel_orders"]:
            room = require(client.get("/api/v1/hotels", headers=one, params=hotels))[0]
            card = require(client.post(f"/api/v1/sessions/{session}/candidate-proposals",
                                       headers=one, json={"kind": "hotel",
                    "candidate_id": room["product_id"], "unit_price": room["nightly_price"],
                    **hotels}), 201)
            if card["status"] != "pending":
                raise RuntimeError("No pending review card")
            if require(client.get("/api/v1/me/itinerary", headers=one))["hotel_orders"]:
                raise RuntimeError("Order changed before confirmation")
            url = f"/api/v1/sessions/{session}/actions/{card['id']}/confirm"
            first = require(client.post(url, headers=one))
            if require(client.post(url, headers=one)) != first:
                raise RuntimeError("Confirmation was not idempotent")
        elif len(before["hotel_orders"]) != 1:
            raise RuntimeError("Unexpected number of prior capacity-test orders")
        if len(require(client.get("/api/v1/me/itinerary", headers=one))["hotel_orders"]) != 1:
            raise RuntimeError("Confirmed order missing")
        if require(client.get("/api/v1/me/itinerary", headers=two))["hotel_orders"]:
            raise RuntimeError("Order crossed account boundary")
        cases = (("/api/v1/flights", flights), ("/api/v1/hotels", hotels),
                 ("/api/v1/rentals", rentals), ("/api/v1/me/itinerary", None))
        timings = {}
        for parallel in (1, 2, 3):
            latencies = []
            for batch in range(args.batches):
                with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
                    futures = [pool.submit(request, client, (one, two)[i % 2],
                                           *cases[(batch + i) % len(cases)])
                               for i in range(parallel)]
                    latencies.extend(future.result() for future in futures)
            timings[str(parallel)] = {"requests": len(latencies),
                                      "median_ms": round(statistics.median(latencies), 2),
                                      "max_ms": max(latencies)}
    proxy_page = proxy_status("/app")
    proxy_docs = proxy_status("/docs")
    if proxy_page != 200 or proxy_docs != 404:
        raise RuntimeError(f"Local proxy check failed: {proxy_page}, {proxy_docs}")
    report = {"date": str(day), "authorized_accounts": 2,
              "card_confirmed": True, "proxy_page": proxy_page,
              "proxy_docs": proxy_docs, "timings": timings,
              "real_model_requests": 0, "real_search_requests": 0}
    RESULT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
