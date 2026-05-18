"""add is_late flag to inbound_observations

Revision ID: d1e2f3a4b5c6
Revises: c85ba6a368c2
Create Date: 2026-04-19 16:00:00.000000

Ticket #90: archived ServiceRequests still accept reports, but the gateway
flags observations received after the SR's period_end so downstream
consumers can distinguish in-window from out-of-window data.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd1e2f3a4b5c6'
down_revision = 'c85ba6a368c2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('inbound_observations', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'is_late', sa.Boolean(), nullable=False, server_default=sa.false()
        ))
        batch_op.create_index(
            batch_op.f('ix_inbound_observations_is_late'),
            ['is_late'], unique=False,
        )


def downgrade():
    with op.batch_alter_table('inbound_observations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_inbound_observations_is_late'))
        batch_op.drop_column('is_late')
