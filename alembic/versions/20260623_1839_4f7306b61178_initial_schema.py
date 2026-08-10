"""initial_schema

Revision ID: 4f7306b61178
Revises:
Create Date: 2026-06-23 18:39:10.026976+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '4f7306b61178'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_integrations_source_jira_expires', 'integrations', ['source', 'jira_webhook_expires_at'], unique=False)
    op.create_index('ix_integrations_source_sub_expires', 'integrations', ['source', 'subscription_expires_at'], unique=False)
    op.create_unique_constraint('uq_linked_identity_profile_provider_workspace', 'linked_identities', ['profile_id', 'provider', 'workspace_label'])
    op.drop_column('summaries', 'workspace')


def downgrade() -> None:
    op.add_column('summaries', sa.Column('workspace', sa.VARCHAR(), autoincrement=False, nullable=True))
    op.drop_constraint('uq_linked_identity_profile_provider_workspace', 'linked_identities', type_='unique')
    op.drop_index('ix_integrations_source_sub_expires', table_name='integrations')
    op.drop_index('ix_integrations_source_jira_expires', table_name='integrations')
