"""add_foundry_thread_id

Revision ID: 356d61a360f8
Revises: c1d2e3f4a5b6
Create Date: 2026-06-26 18:47:14.807555+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '356d61a360f8'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('chat_conversations', sa.Column('foundry_thread_id', sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column('chat_conversations', 'foundry_thread_id')
