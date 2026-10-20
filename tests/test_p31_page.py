"""Page delivery and the authenticated demo date contract."""

import unittest
from datetime import date, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api.travel import router
from config import settings
from db.travel_models import airports, demo_state, metadata, travelers
from main import Server
from utils.dependencies import get_db
from utils.password_hash import get_hashed_password


class PageTests(unittest.TestCase):
    def test_page_is_same_origin_and_has_browser_guards(self):
        server = Server()
        server.init_app()
        with TestClient(server.app) as client:
            page = client.get("/app")
            self.assertEqual(page.status_code, 200)
            self.assertIn('lang="zh-CN"', page.text)
            self.assertIn('/static/app.js', page.text)
            self.assertIn("script-src 'self'", page.headers["content-security-policy"])
            self.assertEqual(page.headers["x-content-type-options"], "nosniff")
            self.assertEqual(page.headers["cache-control"], "no-store")
            self.assertEqual(client.get("/static/app.js").status_code, 200)
            self.assertEqual(client.get("/static/app.css").status_code, 200)
            self.assertEqual(client.get("/static/mark.svg").status_code, 200)

    def test_demo_window_requires_login_and_follows_database_date(self):
        engine = create_engine("sqlite+pysqlite:///:memory:",
                               connect_args={"check_same_thread": False}, poolclass=StaticPool)
        metadata.create_all(engine)
        original_secret = settings.JWT_SECRET_KEY
        settings.JWT_SECRET_KEY = "test-only-secret-which-is-long-enough"
        try:
            with engine.begin() as connection:
                connection.execute(insert(travelers).values(
                    id="page-traveler", username="page-demo",
                    password_hash=get_hashed_password("page-password"),
                    display_name="演示旅客", active=1))
                connection.execute(insert(demo_state).values(
                    id=1, as_of=date(2026, 10, 20), fixture_sha256="a" * 64))
                connection.execute(insert(airports), [
                    {"code": "HAM", "name": "Hamburg Airport", "city": "Hamburg", "timezone": "Europe/Berlin"},
                    {"code": "BSL", "name": "EuroAirport Basel-Mulhouse-Freiburg", "city": "Basel", "timezone": "Europe/Zurich"},
                    {"code": "LHR", "name": "London Heathrow Airport", "city": "London", "timezone": "Europe/London"},
                    {"code": "LGW", "name": "London Gatwick Airport", "city": "London", "timezone": "Europe/London"},
                ])
            app = FastAPI()
            app.include_router(router, prefix="/api/v1")

            def test_db():
                with Session(engine) as session:
                    yield session

            app.dependency_overrides[get_db] = test_db
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/v1/demo-state").status_code, 401)
                self.assertEqual(client.get("/api/v1/locations").status_code, 401)
                login = client.post("/api/v1/login", json={
                    "username": "page-demo", "password": "page-password"})
                self.assertEqual(login.status_code, 200)
                auth = {"Authorization": "Bearer " + login.json()["access_token"]}
                first = client.get("/api/v1/demo-state", headers=auth)
                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.json()["bookable_from"], "2026-10-21")
                self.assertEqual(first.json()["bookable_until"], "2026-11-19")
                self.assertEqual(first.json()["hotel_checkout_until"], "2026-11-20")
                self.assertEqual(first.json()["display_currency"], "CNY")
                locations = client.get("/api/v1/locations", headers=auth)
                self.assertEqual(locations.status_code, 200)
                by_code = {item["code"]: item for item in locations.json()}
                self.assertEqual(by_code["BSL"]["city_zh"], "巴塞尔")
                self.assertEqual(by_code["BSL"]["name_zh"], "巴塞尔米卢斯机场")
                self.assertNotEqual(by_code["LHR"]["name_zh"], by_code["LGW"]["name_zh"])
                self.assertEqual(by_code["HAM"]["city"], "Hamburg")
                with engine.begin() as connection:
                    connection.execute(update(demo_state).values(as_of=date(2026, 11, 1)))
                second = client.get("/api/v1/demo-state", headers=auth)
                self.assertEqual(second.json()["as_of"], "2026-11-01")
                self.assertEqual(second.json()["bookable_from"], "2026-11-02")
                self.assertEqual(second.json()["bookable_until"],
                                 (date(2026, 11, 1) + timedelta(days=30)).isoformat())
        finally:
            settings.JWT_SECRET_KEY = original_secret
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
