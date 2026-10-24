"""Persistent chat history bound to a verified traveler identity."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, insert, select, update

from travel_assistant.db.travel_models import chat_messages, chat_sessions, chat_turns


class SessionNotFound(ValueError):
    """The requested session is absent or belongs to another traveler."""


def _owned_session(db, traveler_id, session_id, *, lock=False):
    query = select(chat_sessions).where(
        chat_sessions.c.id == session_id,
        chat_sessions.c.traveler_id == traveler_id,
    )
    if lock:
        query = query.with_for_update()
    row = db.execute(query).mappings().one_or_none()
    if row is None:
        raise SessionNotFound("session not found")
    return row


def _public_session(row):
    return {key: row[key] for key in ("id", "status", "created_at", "updated_at")}


def create_session(db, traveler_id):
    session_id = str(uuid4())
    db.execute(insert(chat_sessions).values(
        id=session_id, traveler_id=traveler_id, status="active",
    ))
    return _public_session(_owned_session(db, traveler_id, session_id))


def list_sessions(db, traveler_id):
    rows = db.execute(select(chat_sessions).where(
        chat_sessions.c.traveler_id == traveler_id
    ).order_by(chat_sessions.c.updated_at.desc(), chat_sessions.c.id)).mappings()
    return [_public_session(row) for row in rows]


def get_session_history(db, traveler_id, session_id):
    session_row = _owned_session(db, traveler_id, session_id)
    messages = db.execute(select(
        chat_messages.c.sequence, chat_messages.c.role,
        chat_messages.c.content, chat_messages.c.created_at,
    ).where(chat_messages.c.session_id == session_id)
        .order_by(chat_messages.c.sequence)).mappings()
    return {**_public_session(session_row),
            "messages": [dict(row) for row in messages]}


def latest_session_references(db, traveler_id, session_id):
    _owned_session(db, traveler_id, session_id)
    return db.execute(select(chat_turns.c.references).where(
        chat_turns.c.session_id == session_id,
        chat_turns.c.status == "completed",
    ).order_by(chat_turns.c.started_at.desc(), chat_turns.c.id.desc()).limit(1)
    ).scalar_one_or_none() or []


def append_message(db, traveler_id, session_id, role, content):
    if role not in ("user", "assistant"):
        raise ValueError("invalid message role")
    content = content.strip()
    if not 1 <= len(content) <= 4000:
        raise ValueError("message length must be between 1 and 4000")
    owned = _owned_session(db, traveler_id, session_id, lock=True)
    if owned["status"] != "active":
        raise ValueError("session is closed")
    sequence = db.execute(select(func.coalesce(func.max(chat_messages.c.sequence), 0))
                          .where(chat_messages.c.session_id == session_id)).scalar_one() + 1
    db.execute(insert(chat_messages).values(
        session_id=session_id, sequence=sequence, role=role, content=content,
    ))
    db.execute(update(chat_sessions).where(
        chat_sessions.c.id == session_id, chat_sessions.c.traveler_id == traveler_id,
    ).values(updated_at=datetime.now(timezone.utc)))
    return dict(db.execute(select(
        chat_messages.c.sequence, chat_messages.c.role,
        chat_messages.c.content, chat_messages.c.created_at,
    ).where(chat_messages.c.session_id == session_id,
            chat_messages.c.sequence == sequence)).mappings().one())
