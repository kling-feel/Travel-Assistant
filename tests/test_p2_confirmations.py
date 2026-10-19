"""P2.3 confirmation boundaries and transactional order writes."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from db.travel_models import (action_results, audit_events, chat_sessions, flight_offers,
                              flight_orders, hotel_orders, hotel_products, hotels,
                              pending_actions,
                              policy_documents)
from migrations import v004_confirmations
from services.actions import create_proposal
from services.confirmations import confirm_action
from services.policy_catalog import HOTEL_POLICY, HOTEL_POLICY_V3, LEGACY_POLICIES
from test_p2_actions import ActionFixture


class ConfirmationTests(ActionFixture):
    def _action(self, db, kind, params, traveler="t1", chat="session-1"):
        return create_proposal(db, traveler, chat, kind, params, now=self.now)

    def _post(self, client, action, operation, headers, chat="session-1"):
        return client.post(f"/api/v1/sessions/{chat}/actions/{action['id']}/{operation}",
                           headers=headers)

    def test_historical_policy_card_cannot_confirm_under_current_rule(self):
        with Session(self.engine) as db:
            action = self._action(db, "hotel_cancel", {
                "order_id": "h1", "expected_version": 1})
            previous = next(rule for rule in LEGACY_POLICIES
                            if rule["id"] == "hotel-actions-demo-v2")
            db.execute(insert(policy_documents).values(**previous))
            db.execute(update(pending_actions).where(
                pending_actions.c.id == action["id"]).values(
                    policy_id=previous["id"], policy_version=previous["version"]))
            db.commit()
            result = confirm_action(db, "t1", "session-1", action["id"], now=self.now)
            self.assertEqual(result["status"], "invalidated")
            self.assertEqual(db.execute(select(hotel_orders.c.status).where(
                hotel_orders.c.id == "h1")).scalar_one(), "confirmed")

    def test_v3_hotel_card_is_invalidated_without_changing_order(self):
        with Session(self.engine) as db:
            action = self._action(db, "hotel_cancel", {
                "order_id": "h1", "expected_version": 1})
            db.execute(update(pending_actions).where(
                pending_actions.c.id == action["id"]).values(
                    policy_id=HOTEL_POLICY_V3["id"],
                    policy_version=HOTEL_POLICY_V3["version"]))
            db.commit()
            result = confirm_action(db, "t1", "session-1", action["id"], now=self.now)
            self.assertEqual(result["status"], "invalidated")
            self.assertEqual(db.execute(select(hotel_orders.c.status).where(
                hotel_orders.c.id == "h1")).scalar_one(), "confirmed")
            self.assertEqual(db.execute(select(action_results).where(
                action_results.c.action_id == action["id"])).all(), [])

    def test_hotel_refund_closes_at_airport_local_midnight(self):
        check_in = date(2026, 10, 19)
        before = datetime(2026, 10, 18, 21, 59, tzinfo=timezone.utc)
        cutoff = datetime(2026, 10, 18, 22, 0, tzinfo=timezone.utc)
        with Session(self.engine) as db:
            db.execute(update(hotels).where(hotels.c.id == 1).values(
                nearby_airport="BSL"))
            db.execute(update(hotel_products).where(
                hotel_products.c.id == "room-1").values(
                    available_from=check_in,
                    available_until=check_in + timedelta(days=5)))
            db.execute(update(hotel_orders).where(
                hotel_orders.c.id == "h1").values(
                    check_in=check_in, check_out=check_in + timedelta(days=1)))
            action = create_proposal(db, "t1", "session-1", "hotel_cancel", {
                "order_id": "h1", "expected_version": 1}, now=before)
            self.assertEqual(action["policy"],
                             {"id": HOTEL_POLICY["id"], "version": 4})
            self.assertEqual((action["price"]["refundable"],
                              action["price"]["cancellation_fee"]), (80, 0))
            db.commit()
            self.assertEqual(confirm_action(db, "t1", "session-1", action["id"],
                                            now=cutoff)["status"], "invalidated")
            self.assertEqual(db.execute(select(hotel_orders.c.status).where(
                hotel_orders.c.id == "h1")).scalar_one(), "confirmed")
            with self.assertRaises(ValueError):
                create_proposal(db, "t1", "session-1", "hotel_cancel", {
                    "order_id": "h1", "expected_version": 1}, now=cutoff)
            for kind, params in (
                ("hotel_book", {"product_id": "room-1", "check_in": check_in.isoformat(),
                                "check_out": (check_in + timedelta(days=1)).isoformat()}),
                ("hotel_change", {"order_id": "h1", "expected_version": 1,
                                  "product_id": "room-1", "check_in": check_in.isoformat(),
                                  "check_out": (check_in + timedelta(days=2)).isoformat()}),
            ):
                self.assertEqual(create_proposal(db, "t1", "session-1", kind, params,
                                                 now=cutoff)["policy"]["version"], 4)
            db.rollback()
            fresh = create_proposal(db, "t1", "session-1", "hotel_cancel", {
                "order_id": "h1", "expected_version": 1}, now=before)
            db.commit()
            outcome = confirm_action(db, "t1", "session-1", fresh["id"], now=before)
            self.assertEqual(outcome["status"], "confirmed")
            self.assertEqual(outcome["price"]["refundable"], 80)
            self.assertEqual(outcome["order"]["status"], "cancelled")

    def test_five_actions_execute_once_with_order_result_and_audit(self):
        with Session(self.engine) as db:
            actions = [
                self._action(db, "flight_change", {"order_id": "f1", "expected_version": 1,
                                                    "new_flight_id": 2}),
                self._action(db, "flight_cancel", {"order_id": "f2", "expected_version": 1},
                             "t2", "session-2"),
                self._action(db, "hotel_book", {"product_id": "room-1",
                                                  "check_in": self.check_in.isoformat(),
                                                  "check_out": self.check_out.isoformat()}),
                self._action(db, "hotel_change", {"order_id": "h1", "expected_version": 1,
                                                    "product_id": "room-1",
                                                    "check_in": self.check_in.isoformat(),
                                                    "check_out": (self.check_out + timedelta(days=1)).isoformat()}),
                self._action(db, "hotel_cancel", {"order_id": "h2", "expected_version": 1},
                             "t2", "session-2"),
            ]
            db.commit()
        with TestClient(self._app()) as client:
            one = self._auth(client, "demo1", "password1")
            two = self._auth(client, "demo2", "password2")
            responses = [self._post(client, action, "confirm", two if index in (1, 4) else one,
                                    "session-2" if index in (1, 4) else "session-1")
                         for index, action in enumerate(actions)]
            self.assertEqual([r.status_code for r in responses], [200] * 5)
            self.assertEqual(responses[0].json()["order"]["amount"], 120)
            self.assertEqual(responses[0].json()["price"]["payable"], 20)
            self.assertEqual(responses[1].json()["price"]["cancellation_fee"], 32)
            self.assertEqual(responses[1].json()["price"]["refundable"], 73)
            self.assertEqual(responses[2].json()["order"]["total_price"], 80)
            self.assertEqual(responses[3].json()["order"]["total_price"], 160)
            self.assertEqual(responses[4].json()["order"]["status"], "cancelled")
            self.assertEqual(responses[4].json()["price"]["refundable"], 80)
            again = self._post(client, actions[2], "confirm", one)
            self.assertEqual(again.json(), responses[2].json())
            self.assertEqual(self._post(client, actions[2], "reject", one).status_code, 409)
            self.assertEqual(len(client.get("/api/v1/me/action-history", headers=one).json()), 6)
        with Session(self.engine) as db:
            self.assertEqual(db.execute(select(func.count()).select_from(action_results)).scalar_one(), 5)
            self.assertEqual(db.execute(select(func.count()).select_from(audit_events)).scalar_one(), 10)
            self.assertEqual(db.execute(select(func.count()).select_from(hotel_orders)).scalar_one(), 3)
            self.assertEqual(db.execute(select(flight_orders.c.version).where(
                flight_orders.c.id == "f1")).scalar_one(), 2)

    def test_reject_expiry_and_foreign_action_never_write_order(self):
        with Session(self.engine) as db:
            rejected = self._action(db, "flight_cancel", {"order_id": "f1", "expected_version": 1})
            expired = self._action(db, "hotel_cancel", {"order_id": "h1", "expected_version": 1})
            db.execute(update(pending_actions).where(pending_actions.c.id == expired["id"]).values(
                created_at=self.now - timedelta(minutes=20),
                expires_at=self.now - timedelta(minutes=5)))
            db.commit()
        with TestClient(self._app()) as client:
            one = self._auth(client, "demo1", "password1")
            two = self._auth(client, "demo2", "password2")
            self.assertEqual(client.post(
                f"/api/v1/sessions/session-1/actions/{rejected['id']}/confirm",
                headers=one, json={"amount": 0}).status_code, 422)
            self.assertEqual(self._post(client, rejected, "confirm", two).status_code, 404)
            self.assertEqual(self._post(client, rejected, "reject", one).status_code, 200)
            self.assertEqual(self._post(client, rejected, "reject", one).status_code, 200)
            self.assertEqual(self._post(client, rejected, "confirm", one).status_code, 409)
            self.assertEqual(self._post(client, expired, "confirm", one).status_code, 409)
        with Session(self.engine) as db:
            self.assertEqual(db.execute(select(flight_orders.c.status).where(
                flight_orders.c.id == "f1")).scalar_one(), "confirmed")
            self.assertEqual(db.execute(select(hotel_orders.c.status).where(
                hotel_orders.c.id == "h1")).scalar_one(), "confirmed")
            self.assertEqual(db.execute(select(func.count()).select_from(action_results)).scalar_one(), 0)

    def test_version_offer_and_policy_changes_invalidate(self):
        scenarios = (
            ("version", "flight_cancel", {"order_id": "f1", "expected_version": 1}),
            ("offer", "flight_change", {"order_id": "f1", "expected_version": 1,
                                        "new_flight_id": 2}),
            ("offer_version", "flight_change", {"order_id": "f1", "expected_version": 1,
                                                 "new_flight_id": 2}),
            ("offer_rule", "flight_change", {"order_id": "f1", "expected_version": 1,
                                              "new_flight_id": 2}),
            ("policy", "hotel_cancel", {"order_id": "h1", "expected_version": 1}),
        )
        for change, kind, params in scenarios:
            with self.subTest(change=change), Session(self.engine) as db:
                action = self._action(db, kind, params)
                db.commit()
                if change == "version":
                    db.execute(update(flight_orders).where(flight_orders.c.id == "f1").values(version=2))
                elif change == "offer":
                    db.execute(update(flight_offers).where(flight_offers.c.flight_id == 2).values(amount=121))
                elif change == "offer_version":
                    db.execute(update(flight_offers).where(flight_offers.c.flight_id == 2).values(version=2))
                elif change == "offer_rule":
                    db.execute(update(flight_offers).where(flight_offers.c.flight_id == 2).values(rule_id="changed"))
                else:
                    db.execute(update(policy_documents).where(
                        policy_documents.c.id == HOTEL_POLICY["id"]).values(body="changed"))
                db.commit()
                result = confirm_action(db, "t1", "session-1", action["id"], now=self.now)
                self.assertEqual(result["status"], "invalidated")
                db.commit()
                self.assertEqual(db.execute(select(pending_actions.c.status).where(
                    pending_actions.c.id == action["id"])).scalar_one(), "invalidated")
                self.assertEqual(db.execute(select(action_results).where(
                    action_results.c.action_id == action["id"])).all(), [])
                if change == "version":
                    db.execute(update(flight_orders).where(flight_orders.c.id == "f1").values(version=1))
                elif change == "offer":
                    db.execute(update(flight_offers).where(flight_offers.c.flight_id == 2).values(amount=120))
                elif change == "offer_version":
                    db.execute(update(flight_offers).where(flight_offers.c.flight_id == 2).values(version=1))
                elif change == "offer_rule":
                    db.execute(update(flight_offers).where(flight_offers.c.flight_id == 2).values(rule_id="flight-offer-demo-v1"))
                else:
                    db.execute(update(policy_documents).where(
                        policy_documents.c.id == HOTEL_POLICY["id"]).values(body=HOTEL_POLICY["body"]))
                db.commit()

    def test_closed_session_invalidates_saved_action(self):
        with Session(self.engine) as db:
            action = self._action(db, "hotel_book", {"product_id": "room-1",
                                                    "check_in": self.check_in.isoformat(),
                                                    "check_out": self.check_out.isoformat()})
            db.execute(update(chat_sessions).where(chat_sessions.c.id == "session-1")
                       .values(status="closed"))
            db.commit()
            self.assertEqual(confirm_action(db, "t1", "session-1", action["id"],
                                            now=self.now)["status"], "invalidated")
            db.commit()
            self.assertEqual(db.execute(select(action_results).where(
                action_results.c.action_id == action["id"])).all(), [])

    def test_late_audit_failure_rolls_back_order_and_action(self):
        with Session(self.engine) as db:
            action = self._action(db, "flight_change", {"order_id": "f1",
                                                        "expected_version": 1,
                                                        "new_flight_id": 2})
            db.commit()
            with patch("services.confirmations._event", side_effect=RuntimeError("audit failed")):
                with self.assertRaisesRegex(RuntimeError, "audit failed"):
                    confirm_action(db, "t1", "session-1", action["id"], now=self.now)
            db.rollback()
            order = db.execute(select(flight_orders).where(flight_orders.c.id == "f1")).mappings().one()
            self.assertEqual((order["flight_id"], order["amount"], order["version"]), (1, 100, 1))
            self.assertEqual(db.execute(select(pending_actions.c.status).where(
                pending_actions.c.id == action["id"])).scalar_one(), "pending")
            self.assertEqual(db.execute(select(action_results).where(
                action_results.c.action_id == action["id"])).all(), [])

    def test_v004_backfill_and_repeat_preserves_proposed_audit(self):
        with Session(self.engine) as db:
            action = self._action(db, "flight_cancel", {"order_id": "f1", "expected_version": 1})
            db.commit()
            db.execute(delete(audit_events).where(audit_events.c.action_id == action["id"]))
            db.commit()
        with self.engine.begin() as db:
            v004_confirmations.upgrade(db)
            v004_confirmations.upgrade(db)
            self.assertEqual(db.execute(select(func.count()).select_from(audit_events)).scalar_one(), 1)
