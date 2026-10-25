"""Store request counts and safe provider failure categories without prompts or keys."""

from sqlalchemy import text


VERSION = "014_provider_usage"


def upgrade(connection):
    connection.execute(text("""
        CREATE TABLE IF NOT EXISTS provider_usage_events (
            id BIGSERIAL PRIMARY KEY,
            traveler_id VARCHAR(40) REFERENCES travelers(id),
            provider VARCHAR(16) NOT NULL,
            outcome VARCHAR(24) NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_provider_usage_kind CHECK (provider IN ('model', 'search')),
            CONSTRAINT ck_provider_usage_outcome CHECK
                (outcome IN ('success', 'authentication', 'balance', 'limited', 'timeout', 'upstream'))
        )
    """))
    connection.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_provider_usage_owner_time
        ON provider_usage_events (traveler_id, created_at)
    """))
