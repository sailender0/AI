"""add_email_ai_answer_perm

Adds the "email_ai_answer" permission (email an Ask-AI answer). Grants it to all
existing profiles that don't already list it, so nobody loses the feature, and
updates the column default for future inserts.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-23 22:00:00.000000+00:00
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALL = '["email_report","export_my_day","export_analytics","email_ai_answer"]'


def upgrade() -> None:
    op.execute(
        "UPDATE profiles "
        "SET permissions = permissions::jsonb || '[\"email_ai_answer\"]'::jsonb "
        "WHERE NOT (permissions::jsonb ? 'email_ai_answer')"
    )
    op.execute(f"ALTER TABLE profiles ALTER COLUMN permissions SET DEFAULT '{_ALL}'::json")


def downgrade() -> None:
    op.execute(
        "UPDATE profiles "
        "SET permissions = (permissions::jsonb - 'email_ai_answer')::json"
    )
    op.execute(
        "ALTER TABLE profiles ALTER COLUMN permissions "
        "SET DEFAULT '[\"email_report\",\"export_my_day\",\"export_analytics\"]'::json"
    )
