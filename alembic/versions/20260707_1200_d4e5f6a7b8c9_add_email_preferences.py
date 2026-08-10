"""add_email_preferences

Adds the email_preferences table for scheduled email digests.
Safe to run on DBs that already have the table (IF NOT EXISTS guard).

Revision ID: d4e5f6a7b8c9
Revises: 356d61a360f8
Create Date: 2026-07-07 12:00:00.000000+00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "356d61a360f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = sa.inspect(bind).get_table_names()

    if "email_preferences" not in existing:
        op.create_table(
            "email_preferences",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("profile_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("profiles.id"), nullable=False),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("frequency", sa.String(), nullable=False, server_default="daily"),
            sa.Column("hour", sa.Integer(), nullable=False, server_default="9"),
            sa.Column("weekday", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("profile_id", "kind", name="uq_email_pref_profile_kind"),
        )
        op.create_index("ix_email_preferences_profile_id", "email_preferences", ["profile_id"])


def downgrade() -> None:
    op.drop_table("email_preferences")
