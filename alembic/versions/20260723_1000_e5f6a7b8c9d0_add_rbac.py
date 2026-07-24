"""add_rbac

Adds profiles.role and profiles.permissions for role-based access control.
Existing rows get role='user' with ALL permissions granted, so nobody loses
access on deploy — admins revoke from there.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-23 10:00:00.000000+00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULT_PERMS = '\'["email_report","export_my_day","export_analytics"]\''


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("profiles")}

    if "role" not in cols:
        op.add_column("profiles", sa.Column(
            "role", sa.String(), nullable=False, server_default="user",
        ))
    if "permissions" not in cols:
        op.add_column("profiles", sa.Column(
            "permissions", sa.JSON(), nullable=False,
            server_default=sa.text(f"{_DEFAULT_PERMS}::json"),
        ))


def downgrade() -> None:
    op.drop_column("profiles", "permissions")
    op.drop_column("profiles", "role")
