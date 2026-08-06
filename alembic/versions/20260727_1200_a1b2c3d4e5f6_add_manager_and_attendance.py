"""add_manager_and_attendance

Adds profiles.manager_id (self-FK, ON DELETE SET NULL) so a user can report to a
manager, and renames the 'supervisor' role to 'manager' in existing rows. The
consolidated report (now the attendance grid) keeps its existing `consolidated_report`
permission — no permission schema change here.

Historical access_log rows keep their literal actor_role='supervisor'; rewriting
audit history would falsify the trail, so it stays. Only live profiles are renamed.

Revision ID: a1b2c3d4e5f6
Revises: f6a7b8c9d0e1
Create Date: 2026-07-27 12:00:00.000000+00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("profiles")}
    if "manager_id" not in cols:
        op.add_column("profiles", sa.Column("manager_id", postgresql.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            "fk_profiles_manager_id", "profiles", "profiles",
            ["manager_id"], ["id"], ondelete="SET NULL",
        )
        op.create_index("ix_profiles_manager_id", "profiles", ["manager_id"])

    op.execute("UPDATE profiles SET role = 'manager' WHERE role = 'supervisor'")


def downgrade() -> None:
    op.execute("UPDATE profiles SET role = 'supervisor' WHERE role = 'manager'")
    op.drop_index("ix_profiles_manager_id", table_name="profiles")
    op.drop_constraint("fk_profiles_manager_id", "profiles", type_="foreignkey")
    op.drop_column("profiles", "manager_id")
