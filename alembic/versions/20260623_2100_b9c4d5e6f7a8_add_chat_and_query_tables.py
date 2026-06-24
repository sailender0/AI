"""add_chat_and_query_tables

Creates query_log, chat_conversations, and chat_messages tables.
Uses IF NOT EXISTS logic (via SQLAlchemy inspector) so this migration
is safe to run on databases where create_all already created these tables
before Alembic was adopted.

Revision ID: b9c4d5e6f7a8
Revises: 4f7306b61178
Create Date: 2026-06-23 21:00:00.000000+00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b9c4d5e6f7a8"
down_revision: Union[str, None] = "4f7306b61178"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = sa.inspect(bind).get_table_names()

    if "query_log" not in existing:
        op.create_table(
            "query_log",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("profile_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("profiles.id"), nullable=False),
            sa.Column("question", sa.Text, nullable=False),
            sa.Column("filters_json", postgresql.JSON(astext_type=sa.Text())),
            sa.Column("ai_response", sa.Text),
            sa.Column("context_event_ids", postgresql.JSON(astext_type=sa.Text())),
            sa.Column("asked_at", sa.DateTime(timezone=True)),
        )

    if "chat_conversations" not in existing:
        op.create_table(
            "chat_conversations",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("profile_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("profiles.id"), nullable=False),
            sa.Column("title", sa.String(200), nullable=False, server_default="New chat"),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )

    if "chat_messages" not in existing:
        op.create_table(
            "chat_messages",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("chat_conversations.id"), nullable=False),
            sa.Column("role", sa.String(20), nullable=False),
            sa.Column("content", sa.Text, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
        )


def downgrade() -> None:
    op.drop_table("chat_messages")
    op.drop_table("chat_conversations")
    op.drop_table("query_log")
