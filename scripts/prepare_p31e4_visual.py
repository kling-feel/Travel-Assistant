"""Create local-only visual test cards in the isolated P3.1e4 database."""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import settings
from db import engine
from db.travel_models import airports, demo_state, flights, travelers
from services.actions import create_proposal
from services.sessions import create_session
from utils.password_hash import get_hashed_password


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    assert settings.POSTGRES_PORT == 5434 and not settings.DATABASE_URL
    with engine.begin() as db:
        assert db.execute(select(demo_state.c.as_of)).scalar_one() == args.as_of
        traveler_id = db.execute(select(travelers.c.id).where(
            travelers.c.username == "p31e4_visual")).scalar_one_or_none()
        if traveler_id is None:
            traveler_id = str(uuid4())
            db.execute(insert(travelers).values(
                id=traveler_id, username="p31e4_visual",
                password_hash=get_hashed_password("visual-only-pass-2026"),
                display_name="界面验收旅客", active=1))
        session = create_session(db, traveler_id)
        flight = db.execute(select(flights.c.arrival_at, airports.c.timezone).join(
            airports, flights.c.arrival_airport == airports.c.code).where(
            flights.c.id == 3831)).one()
        arrival = flight.arrival_at.astimezone(ZoneInfo(flight.timezone)).date()
        first = create_proposal(db, traveler_id, session["id"], "flight_book",
                                {"flight_id": 3831})
        second = create_proposal(db, traveler_id, session["id"], "hotel_book",
                                 {"product_id": "room-LGW-1",
                                  "check_in": arrival.isoformat(),
                                  "check_out": (arrival + timedelta(days=2)).isoformat()})
    print({"session_id": session["id"], "flight_card": first["id"],
           "hotel_card": second["id"]})


if __name__ == "__main__":
    main()
