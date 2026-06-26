"""add_device_tables

Adds devices and device_tokens tables for the desktop agent.
Safe to run on DBs that already have these tables (IF NOT EXISTS guard).

Revision ID: c1d2e3f4a5b6
Revises: b9c4d5e6f7a8
Create Date: 2026-06-24 18:00:00.000000+00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, None] = "b9c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = sa.inspect(bind).get_table_names()

    if "devices" not in existing:
        op.create_table(
            "devices",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("profile_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("profiles.id"), nullable=False),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("platform", sa.String(20)),
            sa.Column("last_seen", sa.DateTime(timezone=True)),
            sa.Column("registered_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_devices_profile_id", "devices", ["profile_id"])

    if "device_tokens" not in existing:
        op.create_table(
            "device_tokens",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("device_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("devices.id"), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("created_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_device_tokens_hash", "device_tokens", ["token_hash"])


def downgrade() -> None:
    op.drop_table("device_tokens")
    op.drop_table("devices")
