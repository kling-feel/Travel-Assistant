"""Persist one result per confirmed action and a small action audit trail."""

from sqlalchemy import insert, select, text

from travel_assistant.db.travel_models import action_results, audit_events, metadata, pending_actions

VERSION = "004_confirmations"


def upgrade(connection):
    if connection.dialect.name == "postgresql":
        connection.execute(text(
            "ALTER TABLE pending_actions DROP CONSTRAINT IF EXISTS pending_actions_status_check"
        ))
        connection.execute(text(
            "ALTER TABLE pending_actions ADD CONSTRAINT pending_actions_status_check "
            "CHECK (status IN ('pending', 'confirmed', 'rejected', 'expired', 'invalidated'))"
        ))
    metadata.create_all(connection, tables=[action_results, audit_events])
    existing = set(connection.execute(select(audit_events.c.action_id).where(
        audit_events.c.event_type == "proposed")).scalars())
    for row in connection.execute(select(pending_actions)).mappings():
        if row["id"] not in existing:
            connection.execute(insert(audit_events).values(
                action_id=row["id"], traveler_id=row["traveler_id"],
                session_id=row["session_id"], event_type="proposed",
                summary={"action_type": row["action_type"]},
                created_at=row["created_at"],
            ))
