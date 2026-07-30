"""add_assignable_perms

Adds profiles.assignable_perms (JSON list) — the per-manager, admin-controlled set
of permissions a manager may assign to their reports. Defaults to empty: a manager
can delegate nothing until an admin turns it on in the Manager permissions UI.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-27 15:00:00.000000+00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("profiles")}
    if "assignable_perms" not in cols:
        op.add_column("profiles", sa.Column(
            "assignable_perms", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json"),
        ))


def downgrade() -> None:
    op.drop_column("profiles", "assignable_perms")
