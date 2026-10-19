"""Idempotent conversation turns; messages are the display history."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import insert, select, update

from db.travel_models import chat_turns
from services.sessions import _owned_session, append_message


class TurnConflict(ValueError):
    pass


def _public(row):
    return {"request_key": row["request_key"], "status": row["status"],
            "answer": row["assistant_content"], "references": row["references"] or [],
            "actions": row["action_cards"] or [],
            "error_code": row["error_code"]}


def begin_turn(db, traveler_id, session_id, request_key, content, now=None):
    now = now or datetime.now(timezone.utc)
    owned = _owned_session(db, traveler_id, session_id, lock=True)
    if owned["status"] != "active":
        raise TurnConflict("session is closed")
    existing = db.execute(select(chat_turns).where(
        chat_turns.c.session_id == session_id,
        chat_turns.c.request_key == request_key,
    )).mappings().one_or_none()
    if existing:
        if existing["user_content"] != content:
            raise TurnConflict("request key was already used for different content")
        expiry = existing["expires_at"]
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if existing["status"] == "processing" and expiry <= now:
            db.execute(update(chat_turns).where(chat_turns.c.id == existing["id"]).values(
                status="failed", error_code="interrupted", updated_at=now))
            existing = {**existing, "status": "failed", "error_code": "interrupted"}
        return dict(existing), False
    active = db.execute(select(chat_turns).where(
        chat_turns.c.session_id == session_id,
        chat_turns.c.status == "processing",
    )).mappings().one_or_none()
    if active:
        expiry = active["expires_at"]
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry > now:
            raise TurnConflict("another turn is processing")
        db.execute(update(chat_turns).where(chat_turns.c.id == active["id"]).values(
            status="failed", error_code="interrupted", updated_at=now))
    append_message(db, traveler_id, session_id, "user", content)
    turn_id = str(uuid4())
    db.execute(insert(chat_turns).values(
        id=turn_id, session_id=session_id, request_key=request_key,
        user_content=content, status="processing",
        started_at=now, expires_at=now + timedelta(minutes=2), updated_at=now,
    ))
    return {"id": turn_id, "status": "processing"}, True


def lock_processing_turn(db, traveler_id, session_id, turn_id):
    _owned_session(db, traveler_id, session_id, lock=True)
    row = db.execute(select(chat_turns).where(
        chat_turns.c.id == turn_id, chat_turns.c.session_id == session_id,
        chat_turns.c.status == "processing",
    ).with_for_update()).mappings().one_or_none()
    if row is None:
        raise TurnConflict("turn is no longer processing")
    return row


def finish_turn(db, traveler_id, session_id, turn_id, answer, references, actions=()):
    row = lock_processing_turn(db, traveler_id, session_id, turn_id)
    append_message(db, traveler_id, session_id, "assistant", answer)
    db.execute(update(chat_turns).where(chat_turns.c.id == turn_id).values(
        status="completed", assistant_content=answer, references=references,
        action_cards=list(actions),
        updated_at=datetime.now(timezone.utc)))
    return {"request_key": row["request_key"], "status": "completed",
            "answer": answer, "references": references, "actions": list(actions),
            "error_code": None}


def fail_turn(db, session_id, turn_id, code):
    db.execute(update(chat_turns).where(chat_turns.c.id == turn_id,
                                        chat_turns.c.session_id == session_id,
                                        chat_turns.c.status == "processing").values(
        status="failed", error_code=code,
        updated_at=datetime.now(timezone.utc)))


def has_graph_turns(db, session_id):
    from sqlalchemy import inspect
    if not inspect(db.get_bind()).has_table("chat_turns"):
        return False
    return db.execute(select(chat_turns.c.id).where(
        chat_turns.c.session_id == session_id).limit(1)).first() is not None
