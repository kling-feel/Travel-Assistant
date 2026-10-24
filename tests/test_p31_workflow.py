"""A browser-style two-card journey using disposable data and a fixed model."""

import unittest
from datetime import timedelta

from fastapi.testclient import TestClient

from travel_assistant.core.config import settings
from tests.p31_preview_server import preview_app


class PageWorkflowTests(unittest.TestCase):
    def test_two_cards_are_separate_and_history_is_owned(self):
        previous_secret = settings.JWT_SECRET_KEY
        app, engine, model, saver, day = preview_app()
        try:
            with model, saver, TestClient(app) as client:
                def login(username, password):
                    result = client.post("/api/v1/login", json={
                        "username": username, "password": password})
                    self.assertEqual(result.status_code, 200)
                    return {"Authorization": "Bearer " + result.json()["access_token"]}

                one = login("demo1", "password1")
                two = login("demo2", "password2")
                date_text = day.isoformat()
                self.assertEqual(client.get("/api/v1/demo-state", headers=one)
                                 .json()["bookable_from"], date_text)
                flights = client.get("/api/v1/flights", headers=one, params={
                    "departure": "HAM", "arrival": "BSL", "travel_date": date_text})
                self.assertEqual([item["id"] for item in flights.json()], [1, 2])
                hotels = client.get("/api/v1/hotels", headers=one, params={
                    "city": "Basel", "check_in": date_text,
                    "check_out": (day + timedelta(days=1)).isoformat()})
                self.assertEqual([item["product_id"] for item in hotels.json()], ["room-1"])
                session = client.post("/api/v1/sessions", headers=one, json={}).json()["id"]
                prompt = f"把我的机票改签到第二班，并订第一家酒店，{day.month}月{day.day}日入住一晚"
                url = f"/api/v1/sessions/{session}/turns"
                payload = {"content": prompt, "request_key": "page-workflow-001"}
                turn = client.post(url, headers=one, json=payload)
                self.assertEqual(turn.status_code, 200, turn.text)
                self.assertEqual(len(turn.json()["actions"]), 2, turn.text)
                self.assertEqual(client.post(url, headers=one, json=payload).json(), turn.json())
                self.assertEqual(client.get(f"/api/v1/sessions/{session}", headers=two).status_code, 404)
                self.assertEqual(client.get(f"/api/v1/sessions/{session}/actions", headers=two).status_code, 404)
                before = client.get("/api/v1/me/itinerary", headers=one).json()
                self.assertEqual(before["flight_orders"][0]["flight_no"], "SIM-1")
                self.assertEqual(before["hotel_orders"], [])
                cards = turn.json()["actions"]
                flight = next(item for item in cards if item["action_type"] == "flight_change")
                hotel = next(item for item in cards if item["action_type"] == "hotel_book")
                self.assertIn("departure_local", flight["before"])
                self.assertIn("departure_local", flight["after"])
                for card in (flight, hotel):
                    detail = client.get(f"/api/v1/sessions/{session}/actions/{card['id']}", headers=one)
                    self.assertEqual(detail.json()["status"], "pending")
                    self.assertEqual(client.get(f"/api/v1/sessions/{session}/actions/{card['id']}",
                                                headers=two).status_code, 404)
                flight_url = f"/api/v1/sessions/{session}/actions/{flight['id']}/confirm"
                first = client.post(flight_url, headers=one)
                self.assertEqual(first.status_code, 200, first.text)
                middle = client.get("/api/v1/me/itinerary", headers=one).json()
                self.assertEqual(middle["flight_orders"][0]["flight_no"], "SIM-2")
                self.assertEqual(middle["hotel_orders"], [])
                self.assertEqual(client.post(flight_url, headers=one).json(), first.json())
                hotel_url = f"/api/v1/sessions/{session}/actions/{hotel['id']}/confirm"
                self.assertEqual(client.post(hotel_url, headers=one).status_code, 200)
                after = client.get("/api/v1/me/itinerary", headers=one).json()
                self.assertEqual(len(after["hotel_orders"]), 1)
                self.assertEqual(client.get("/api/v1/me/itinerary", headers=two)
                                 .json()["hotel_orders"], [])
                self.assertTrue(client.get("/api/v1/me/action-history", headers=one).json())
                relogin = login("demo1", "password1")
                self.assertEqual(len(client.get(f"/api/v1/sessions/{session}", headers=relogin)
                                     .json()["messages"]), 2)
        finally:
            settings.JWT_SECRET_KEY = previous_secret
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
