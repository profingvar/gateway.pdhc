"""audit_log: add nullable session_id column (ticket #222)

Revision ID: a7b8c9d0e1f2
Revises: f3a4b5c6d7e8
Create Date: 2026-06-04 14:00:00.000000

Propagates the SSO ``session_id`` (the ``sid`` JWT claim — see
ticket #191) into gateway's existing audit_log so multi-request
reads chain back to one operator session — closing the
Lag (2022:913) chain-of-custody loop on the gateway side.

Nullable so legacy callers (sim.pdhc / monitor.pdhc service-key
calls without an ``X-Operator-Session-Id`` header) keep working.
Indexed because the typical PDL kontroller query is "all rows for
session S".
"""
from alembic import op
import sqlalchemy as sa


revision = 'a7b8c9d0e1f2'
down_revision = 'f3a4b5c6d7e8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.add_column(sa.Column('session_id', sa.String(length=128),
                                      nullable=True))
        batch_op.create_index('ix_audit_log_session_id', ['session_id'],
                              unique=False)


def downgrade():
    with op.batch_alter_table('audit_log', schema=None) as batch_op:
        batch_op.drop_index('ix_audit_log_session_id')
        batch_op.drop_column('session_id')
